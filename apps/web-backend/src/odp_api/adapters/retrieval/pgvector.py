"""Tenant-safe pgvector-shaped retrieval adapter with deterministic local scoring."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

from odp_api.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument, KnowledgeParentChunk
from odp_api.ports.retrieval import RetrievalFilters, RetrievedChunk

# The production implementation must bind ``organization_id``; this adapter's
# deterministic in-memory storage is used by tests until PostgreSQL migrations exist.
POSTGRES_HYBRID_SEARCH_SQL = """
SELECT chunk_id, parent_chunk_id, document_id, organization_id, document_version,
       source_name, text, page_number, paragraph_number,
       vector_score, bm25_score
FROM knowledge_chunk_index
WHERE organization_id = %(organization_id)s
  AND document_status = %(document_status)s
  AND (%(line_id)s IS NULL OR applicable_line_id IS NULL OR applicable_line_id = %(line_id)s)
  AND (%(product_category)s IS NULL OR product_category IS NULL OR product_category = %(product_category)s)
ORDER BY combined_score DESC
LIMIT %(limit)s
"""

_VECTOR_DIMENSIONS = 64
_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)


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
            and (filters.line_id is None or document.applicable_line_id in {None, filters.line_id})
            and (
                filters.product_category is None
                or document.product_category in {None, filters.product_category}
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
    values = [0.0] * _VECTOR_DIMENSIONS
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = digest[0] % _VECTOR_DIMENSIONS
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
