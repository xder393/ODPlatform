"""Deterministic in-memory knowledge index for local demos, E2E tests and CI.

Mirrors the pgvector provider exactly: the same supersede-on-index behavior,
the same tenant-scope filtering semantics (the Python translation of the
``scoped`` WHERE clause in ``POSTGRES_HYBRID_SEARCH_SQL``) and the same
deterministic token-hash + BM25 scoring, so results stay comparable across
backends without requiring PostgreSQL.
"""

from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

from odp_api.adapters.retrieval.pgvector import (
    _bm25_scores,
    _cosine_similarity,
    _normalize,
    _tokens,
)
from odp_api.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument, KnowledgeParentChunk
from odp_api.ports.retrieval import RetrievalFilters, RetrievedChunk


class InMemoryKnowledgeIndex:
    """KnowledgeIndexPort adapter whose search path always applies tenant scope."""

    def __init__(self) -> None:
        self._documents: dict[UUID, KnowledgeDocument] = {}
        self._parents: dict[UUID, KnowledgeParentChunk] = {}
        self._chunks: dict[UUID, KnowledgeChunk] = {}

    def find_indexed_document(
        self,
        organization_id: UUID,
        source_name: str,
        content_sha256: str,
    ) -> KnowledgeDocument | None:
        matches = (
            document
            for document in self._documents.values()
            if document.organization_id == organization_id
            and document.source_name == source_name
            and document.content_sha256 == content_sha256
            and document.status == "INDEXED"
        )
        return max(matches, key=lambda document: document.version, default=None)

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

    def documents(self) -> tuple[KnowledgeDocument, ...]:
        return tuple(self._documents.values())

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
        # Keep this in sync with the ``scoped`` WHERE clause of
        # ``POSTGRES_HYBRID_SEARCH_SQL`` in adapters/retrieval/pgvector.py.
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
