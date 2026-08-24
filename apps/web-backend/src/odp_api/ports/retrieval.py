"""Ports for tenant-scoped knowledge indexing and hybrid retrieval."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from odp_api.modules.knowledge.models import (
    EvidenceKind,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeParentChunk,
)


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """Required scope constraints in addition to the calling organization."""

    document_status: Literal["INDEXED", "SUPERSEDED", "FAILED"] = "INDEXED"
    evidence_kind: EvidenceKind | None = None
    require_evidence_kind: bool = False
    line_id: UUID | None = None
    require_exact_line_scope: bool = False
    product_category: str | None = None
    require_exact_product_scope: bool = False
    exclude_product_category: str | None = None
    limit: int = 8

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("Retrieval limit must be positive.")
        if self.require_exact_line_scope and self.line_id is None:
            raise ValueError("Exact line scope requires a line_id.")
        if self.require_exact_product_scope and self.product_category is None:
            raise ValueError("Exact product scope requires a product_category.")
        if self.product_category is not None and self.exclude_product_category is not None:
            raise ValueError("Product scope cannot include and exclude a category together.")


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A source-addressable child chunk returned by hybrid retrieval."""

    chunk_id: UUID
    parent_chunk_id: UUID
    document_id: UUID
    organization_id: UUID
    document_version: int
    source_name: str
    text: str
    page_number: int
    paragraph_number: int
    vector_score: float
    bm25_score: float
    score: float
    evidence_kind: EvidenceKind | None = None
    applicable_line_id: UUID | None = None
    product_category: str | None = None


class RAGRetrievalPort(Protocol):
    """A retrieval provider that is never allowed to infer tenant scope."""

    def search(
        self,
        query: str,
        organization_id: UUID,
        filters: RetrievalFilters,
    ) -> list[RetrievedChunk]: ...


class KnowledgeIndexPort(RAGRetrievalPort, Protocol):
    """The persistence boundary used by document ingestion."""

    def find_indexed_document(
        self,
        organization_id: UUID,
        source_name: str,
        content_sha256: str,
    ) -> KnowledgeDocument | None: ...

    def next_version(self, organization_id: UUID, source_name: str) -> int: ...

    def index(
        self,
        document: KnowledgeDocument,
        parents: Sequence[KnowledgeParentChunk],
        chunks: Sequence[KnowledgeChunk],
    ) -> None: ...

    def record_failure(self, document: KnowledgeDocument) -> None: ...
