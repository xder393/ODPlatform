"""Tenant-safe pgvector-shaped retrieval adapter with deterministic local scoring."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Protocol
from uuid import UUID

from odp_api.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument, KnowledgeParentChunk
from odp_api.ports.retrieval import RetrievalFilters, RetrievedChunk

POSTGRES_HYBRID_SEARCH_SQL = """
WITH scoped AS (
    SELECT c.*, d.version AS document_version, d.source_name, d.status AS document_status,
           d.evidence_kind, d.applicable_line_id, d.product_category,
           1 - (c.embedding <=> CAST(%(query_embedding)s AS vector)) AS vector_raw,
           cardinality(regexp_split_to_array(lower(c.text), '[^[:alnum:]_]+')) AS document_length
    FROM knowledge_chunk_index AS c
    JOIN knowledge_documents AS d ON d.document_id = c.document_id
    WHERE c.organization_id = %(organization_id)s
      AND d.organization_id = %(organization_id)s
      AND d.status = %(document_status)s
      AND (%(evidence_kind)s IS NULL OR d.evidence_kind = %(evidence_kind)s)
      AND (NOT %(require_evidence_kind)s OR d.evidence_kind IS NOT NULL)
      AND (
          %(line_id)s IS NULL
          OR d.applicable_line_id = %(line_id)s
          OR (NOT %(require_exact_line_scope)s AND d.applicable_line_id IS NULL)
      )
      AND (
          %(product_category)s IS NULL
          OR d.product_category = %(product_category)s
          OR (NOT %(require_exact_product_scope)s AND d.product_category IS NULL)
      )
      AND (
          %(exclude_product_category)s IS NULL
          OR (
              d.product_category IS NOT NULL
              AND d.product_category <> %(exclude_product_category)s
          )
      )
),
query_terms AS (
    SELECT term, count(*) AS query_frequency
    FROM regexp_split_to_table(lower(%(query)s), '[^[:alnum:]_]+') AS term
    WHERE term <> ''
    GROUP BY term
),
term_frequencies AS (
    SELECT s.chunk_id, q.term, q.query_frequency, count(token.term) AS term_frequency
    FROM scoped AS s
    CROSS JOIN query_terms AS q
    LEFT JOIN LATERAL regexp_split_to_table(lower(s.text), '[^[:alnum:]_]+') AS token(term)
        ON token.term = q.term
    GROUP BY s.chunk_id, q.term, q.query_frequency
),
document_frequencies AS (
    SELECT term, count(*) FILTER (WHERE term_frequency > 0) AS document_frequency
    FROM term_frequencies
    GROUP BY term
),
statistics AS (
    SELECT count(*)::double precision AS document_count,
           avg(document_length)::double precision AS average_document_length
    FROM scoped
),
bm25 AS (
    SELECT s.chunk_id, coalesce(sum(
        tf.query_frequency * ln(1 + (statistics.document_count - df.document_frequency + 0.5) / (df.document_frequency + 0.5))
        * tf.term_frequency * 2.5
        / nullif(tf.term_frequency + 1.5 * (1 - 0.75 + 0.75 * s.document_length / nullif(statistics.average_document_length, 0)), 0)
    ), 0) AS bm25_raw
    FROM scoped AS s
    CROSS JOIN statistics
    LEFT JOIN term_frequencies AS tf ON tf.chunk_id = s.chunk_id
    LEFT JOIN document_frequencies AS df ON df.term = tf.term
    GROUP BY s.chunk_id, s.document_length
),
raw_scores AS (
    SELECT s.*, bm25.bm25_raw FROM scoped AS s JOIN bm25 ON bm25.chunk_id = s.chunk_id
),
bounds AS (
    SELECT min(vector_raw) AS min_vector, max(vector_raw) AS max_vector,
           min(bm25_raw) AS min_bm25, max(bm25_raw) AS max_bm25
    FROM raw_scores
),
normalized AS (
    SELECT raw_scores.*,
           CASE WHEN bounds.max_vector = bounds.min_vector THEN CASE WHEN bounds.max_vector > 0 THEN 1 ELSE 0 END
                ELSE (vector_raw - bounds.min_vector) / (bounds.max_vector - bounds.min_vector) END AS vector_score,
           CASE WHEN bounds.max_bm25 = bounds.min_bm25 THEN CASE WHEN bounds.max_bm25 > 0 THEN 1 ELSE 0 END
                ELSE (bm25_raw - bounds.min_bm25) / (bounds.max_bm25 - bounds.min_bm25) END AS bm25_score
    FROM raw_scores CROSS JOIN bounds
)
SELECT chunk_id, parent_chunk_id, document_id, organization_id, document_version,
       source_name, evidence_kind, applicable_line_id, product_category,
       text, page_number, paragraph_number, vector_score, bm25_score,
       (vector_score + bm25_score) / 2 AS combined_score
FROM normalized
ORDER BY combined_score DESC, chunk_id ASC
LIMIT %(limit)s
"""

# Keep this synchronized with ``embedding vector(64)`` in migration 0001.
PGVECTOR_EMBEDDING_DIMENSIONS = 64
_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)


class PostgresExecutorPort(Protocol):
    """The small parameterized database surface needed by the pgvector provider."""

    def execute(self, sql: str, parameters: Mapping[str, object]) -> None: ...

    def fetch_all(self, sql: str, parameters: Mapping[str, object]) -> Sequence[Mapping[str, object]]: ...


class PgVectorPostgresAdapter:
    """Production pgvector persistence and hybrid retrieval provider."""

    def __init__(
        self,
        executor: PostgresExecutorPort,
        *,
        embed: Callable[[str], Sequence[float]],
    ) -> None:
        self._executor = executor
        self._embed = embed

    def next_version(self, organization_id: UUID, source_name: str) -> int:
        rows = self._executor.fetch_all(
            """SELECT COALESCE(MAX(version), 0) + 1 AS version
               FROM knowledge_documents
               WHERE organization_id = %(organization_id)s AND source_name = %(source_name)s""",
            {"organization_id": organization_id, "source_name": source_name},
        )
        return int(rows[0]["version"]) if rows else 1

    def index(
        self,
        document: KnowledgeDocument,
        parents: Sequence[KnowledgeParentChunk],
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        PgVectorRetrievalAdapter._validate_index_scope(document, parents, chunks)
        chunk_parameters = [
            {
                "chunk_id": chunk.chunk_id,
                "parent_chunk_id": chunk.parent_chunk_id,
                "document_id": chunk.document_id,
                "organization_id": chunk.organization_id,
                "text": chunk.text,
                "page_number": chunk.page_number,
                "paragraph_number": chunk.paragraph_number,
                "child_index": chunk.child_index,
                "embedding": _vector_parameter(self._embed(chunk.text)),
            }
            for chunk in chunks
        ]
        document_parameters = _document_parameters(document)
        self._executor.execute(
            """UPDATE knowledge_documents SET status = 'SUPERSEDED'
               WHERE organization_id = %(organization_id)s AND source_name = %(source_name)s
                 AND status = 'INDEXED'""",
            document_parameters,
        )
        self._persist_document(document, document_parameters)
        for parent in parents:
            self._executor.execute(
                """INSERT INTO knowledge_parent_chunks (
                       parent_chunk_id, document_id, organization_id, text, page_number, paragraph_number
                   ) VALUES (%(parent_chunk_id)s, %(document_id)s, %(organization_id)s, %(text)s,
                             %(page_number)s, %(paragraph_number)s)""",
                {
                    "parent_chunk_id": parent.parent_chunk_id,
                    "document_id": parent.document_id,
                    "organization_id": parent.organization_id,
                    "text": parent.text,
                    "page_number": parent.page_number,
                    "paragraph_number": parent.paragraph_number,
                },
            )
        for parameters in chunk_parameters:
            self._executor.execute(
                """INSERT INTO knowledge_chunk_index (
                       chunk_id, parent_chunk_id, document_id, organization_id, text, page_number,
                       paragraph_number, child_index, embedding
                   ) VALUES (%(chunk_id)s, %(parent_chunk_id)s, %(document_id)s, %(organization_id)s,
                             %(text)s, %(page_number)s, %(paragraph_number)s, %(child_index)s,
                             CAST(%(embedding)s AS vector))""",
                parameters,
            )

    def record_failure(self, document: KnowledgeDocument) -> None:
        if document.status != "FAILED":
            raise ValueError("Only failed documents may be recorded as ingestion failures.")
        self._persist_document(document, _document_parameters(document))

    def search(
        self,
        query: str,
        organization_id: UUID,
        filters: RetrievalFilters,
    ) -> list[RetrievedChunk]:
        if not isinstance(organization_id, UUID):
            raise TypeError("organization_id is required for retrieval.")
        if not isinstance(filters, RetrievalFilters):
            raise TypeError("RetrievalFilters are required for retrieval.")
        rows = self._executor.fetch_all(
            POSTGRES_HYBRID_SEARCH_SQL,
            {
                "query": query,
                "query_embedding": _vector_parameter(self._embed(query)),
                "organization_id": organization_id,
                "document_status": filters.document_status,
                "evidence_kind": filters.evidence_kind,
                "require_evidence_kind": filters.require_evidence_kind,
                "line_id": filters.line_id,
                "require_exact_line_scope": filters.require_exact_line_scope,
                "product_category": filters.product_category,
                "require_exact_product_scope": filters.require_exact_product_scope,
                "exclude_product_category": filters.exclude_product_category,
                "limit": filters.limit,
            },
        )
        return [_retrieved_chunk_from_row(row) for row in rows]

    def _persist_document(
        self, document: KnowledgeDocument, parameters: Mapping[str, object]
    ) -> None:
        self._executor.execute(
            """INSERT INTO knowledge_documents (
                   document_id, organization_id, source_name, filename, version, media_type,
                   content_sha256, status, indexed_at, evidence_kind, applicable_line_id,
                   product_category, failure_reason
               ) VALUES (%(document_id)s, %(organization_id)s, %(source_name)s, %(filename)s, %(version)s,
                         %(media_type)s, %(content_sha256)s, %(status)s, %(indexed_at)s,
                         %(evidence_kind)s, %(applicable_line_id)s, %(product_category)s,
                         %(failure_reason)s)""",
            parameters,
        )


def _document_parameters(document: KnowledgeDocument) -> dict[str, object]:
    return {
        "document_id": document.document_id,
        "organization_id": document.organization_id,
        "source_name": document.source_name,
        "filename": document.filename,
        "version": document.version,
        "media_type": document.media_type,
        "content_sha256": document.content_sha256,
        "status": document.status,
        "indexed_at": document.indexed_at,
        "evidence_kind": document.evidence_kind,
        "applicable_line_id": document.applicable_line_id,
        "product_category": document.product_category,
        "failure_reason": document.failure_reason,
    }


def _vector_parameter(vector: Sequence[float]) -> str:
    if len(vector) != PGVECTOR_EMBEDDING_DIMENSIONS:
        raise ValueError(f"Embeddings must contain exactly {PGVECTOR_EMBEDDING_DIMENSIONS} dimensions.")
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("Embeddings must contain finite values.")
    return json.dumps(list(vector), separators=(",", ":"))


def _retrieved_chunk_from_row(row: Mapping[str, object]) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=UUID(str(row["chunk_id"])),
        parent_chunk_id=UUID(str(row["parent_chunk_id"])),
        document_id=UUID(str(row["document_id"])),
        organization_id=UUID(str(row["organization_id"])),
        document_version=int(row["document_version"]),
        source_name=str(row["source_name"]),
        text=str(row["text"]),
        page_number=int(row["page_number"]),
        paragraph_number=int(row["paragraph_number"]),
        vector_score=float(row["vector_score"]),
        bm25_score=float(row["bm25_score"]),
        score=float(row["combined_score"]),
        evidence_kind=row["evidence_kind"],
        applicable_line_id=(
            UUID(str(row["applicable_line_id"]))
            if row["applicable_line_id"] is not None
            else None
        ),
        product_category=(
            str(row["product_category"])
            if row["product_category"] is not None
            else None
        ),
    )


class PgVectorRetrievalAdapter:
    """Index adapter whose search path always receives and applies organization scope."""

    def __init__(self) -> None:
        self._documents: dict[UUID, KnowledgeDocument] = {}
        self._parents: dict[UUID, KnowledgeParentChunk] = {}
        self._chunks: dict[UUID, KnowledgeChunk] = {}

    def next_version(self, organization_id: UUID, source_name: str) -> int:
        versions = [
            document.version
            for document in self._documents.values()
            if document.organization_id == organization_id and document.source_name == source_name
        ]
        return max(versions, default=0) + 1

    def index(
        self,
        document: KnowledgeDocument,
        parents: Sequence[KnowledgeParentChunk],
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        self._validate_index_scope(document, parents, chunks)
        for existing in tuple(self._documents.values()):
            if (
                existing.organization_id == document.organization_id
                and existing.source_name == document.source_name
                and existing.status == "INDEXED"
            ):
                self._documents[existing.document_id] = replace(existing, status="SUPERSEDED")
        self._documents[document.document_id] = document
        self._parents.update({parent.parent_chunk_id: parent for parent in parents})
        self._chunks.update({chunk.chunk_id: chunk for chunk in chunks})

    def record_failure(self, document: KnowledgeDocument) -> None:
        if document.status != "FAILED":
            raise ValueError("Only failed documents may be recorded as ingestion failures.")
        self._documents[document.document_id] = document

    def document(self, document_id: UUID) -> KnowledgeDocument:
        return self._documents[document_id]

    def chunks_for_document(self, document_id: UUID) -> list[KnowledgeChunk]:
        return sorted(
            (chunk for chunk in self._chunks.values() if chunk.document_id == document_id),
            key=lambda chunk: (chunk.page_number, chunk.paragraph_number, chunk.child_index),
        )

    def search(
        self,
        query: str,
        organization_id: UUID,
        filters: RetrievalFilters,
    ) -> list[RetrievedChunk]:
        if not isinstance(organization_id, UUID):
            raise TypeError("organization_id is required for retrieval.")
        if not isinstance(filters, RetrievalFilters):
            raise TypeError("RetrievalFilters are required for retrieval.")
        candidates = [
            chunk
            for chunk in self._chunks.values()
            if self._matches_required_scope(chunk, organization_id, filters)
        ]
        if not candidates:
            return []
        query_tokens = _tokens(query)
        vector_scores = [_cosine_similarity(query_tokens, _tokens(chunk.text)) for chunk in candidates]
        bm25_scores = _bm25_scores(query_tokens, [_tokens(chunk.text) for chunk in candidates])
        normal_vector = _normalize(vector_scores)
        normal_bm25 = _normalize(bm25_scores)
        results = [
            self._retrieved_chunk(chunk, vector, bm25, (vector + bm25) / 2)
            for chunk, vector, bm25 in zip(candidates, normal_vector, normal_bm25)
        ]
        return sorted(results, key=lambda result: (-result.score, str(result.chunk_id)))[: filters.limit]

    def _matches_required_scope(
        self,
        chunk: KnowledgeChunk,
        organization_id: UUID,
        filters: RetrievalFilters,
    ) -> bool:
        document = self._documents.get(chunk.document_id)
        return bool(
            document
            and chunk.organization_id == organization_id
            and document.organization_id == organization_id
            and document.status == filters.document_status
            and (
                filters.evidence_kind is None
                or document.evidence_kind == filters.evidence_kind
            )
            and (not filters.require_evidence_kind or document.evidence_kind is not None)
            and (
                filters.line_id is None
                or document.applicable_line_id == filters.line_id
                or (
                    not filters.require_exact_line_scope
                    and document.applicable_line_id is None
                )
            )
            and (
                filters.product_category is None
                or document.product_category == filters.product_category
                or (
                    not filters.require_exact_product_scope
                    and document.product_category is None
                )
            )
            and (
                filters.exclude_product_category is None
                or (
                    document.product_category is not None
                    and document.product_category != filters.exclude_product_category
                )
            )
        )

    def _retrieved_chunk(
        self,
        chunk: KnowledgeChunk,
        vector_score: float,
        bm25_score: float,
        score: float,
    ) -> RetrievedChunk:
        document = self._documents[chunk.document_id]
        return RetrievedChunk(
            chunk_id=chunk.chunk_id,
            parent_chunk_id=chunk.parent_chunk_id,
            document_id=document.document_id,
            organization_id=document.organization_id,
            document_version=document.version,
            source_name=document.source_name,
            text=chunk.text,
            page_number=chunk.page_number,
            paragraph_number=chunk.paragraph_number,
            vector_score=vector_score,
            bm25_score=bm25_score,
            score=score,
            evidence_kind=document.evidence_kind,
            applicable_line_id=document.applicable_line_id,
            product_category=document.product_category,
        )

    @staticmethod
    def _validate_index_scope(
        document: KnowledgeDocument,
        parents: Sequence[KnowledgeParentChunk],
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        if document.status != "INDEXED":
            raise ValueError("Only indexed documents may be added to a retrieval index.")
        parent_ids = {parent.parent_chunk_id for parent in parents}
        if any(
            parent.document_id != document.document_id
            or parent.organization_id != document.organization_id
            for parent in parents
        ) or any(
            chunk.document_id != document.document_id
            or chunk.organization_id != document.organization_id
            or chunk.parent_chunk_id not in parent_ids
            for chunk in chunks
        ):
            raise ValueError("Every index row must belong to the document organization.")


def _tokens(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def _vector(tokens: Sequence[str]) -> list[float]:
    values = [0.0] * PGVECTOR_EMBEDDING_DIMENSIONS
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = digest[0] % PGVECTOR_EMBEDDING_DIMENSIONS
        values[index] += 1.0 if digest[1] % 2 else -1.0
    return values


def _cosine_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    left_vector, right_vector = _vector(left), _vector(right)
    numerator = sum(a * b for a, b in zip(left_vector, right_vector))
    denominator = math.sqrt(sum(a * a for a in left_vector) * sum(b * b for b in right_vector))
    return numerator / denominator if denominator else 0.0


def _bm25_scores(query_tokens: Sequence[str], documents: Sequence[Sequence[str]]) -> list[float]:
    if not documents or not query_tokens:
        return [0.0] * len(documents)
    average_length = sum(len(document) for document in documents) / len(documents)
    query_counts = Counter(query_tokens)
    scores: list[float] = []
    for document in documents:
        term_counts = Counter(document)
        score = 0.0
        for term, query_frequency in query_counts.items():
            document_frequency = sum(term in candidate for candidate in documents)
            inverse_frequency = math.log(1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5))
            frequency = term_counts[term]
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(document) / average_length)
            score += query_frequency * inverse_frequency * frequency * 2.5 / denominator if denominator else 0.0
        scores.append(score)
    return scores


def _normalize(scores: Sequence[float]) -> list[float]:
    maximum = max(scores, default=0.0)
    minimum = min(scores, default=0.0)
    if maximum == minimum:
        return [1.0 if maximum > 0 else 0.0 for _ in scores]
    return [(score - minimum) / (maximum - minimum) for score in scores]


class PsycopgPostgresExecutor:
    """Parameterized executor over one PostgreSQL connection URL."""

    def __init__(self, postgres_url: str) -> None:
        self._postgres_url = postgres_url

    def execute(self, sql: str, parameters: Mapping[str, object]) -> None:
        import psycopg  # deferred optional runtime dependency

        with psycopg.connect(self._postgres_url) as connection:
            connection.execute(sql, parameters)

    def fetch_all(
        self, sql: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        import psycopg  # deferred optional runtime dependency
        from psycopg.rows import dict_row

        with psycopg.connect(self._postgres_url, row_factory=dict_row) as connection:
            return list(connection.execute(sql, parameters))


def psycopg_executor(postgres_url: str) -> PostgresExecutorPort:
    """Build an executor on a PostgreSQL connection URL.

    ``psycopg`` is imported inside the executor methods on purpose: it is only
    required when the pgvector retrieval backend runs against a real database.
    Local tests and the in-memory backend never load the driver.
    """
    return PsycopgPostgresExecutor(postgres_url)
