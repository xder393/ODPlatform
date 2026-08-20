"""Ports for tenant-scoped knowledge indexing and hybrid retrieval."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from odp_api.modules.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeParentChunk,
)


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """Required scope constraints in addition to the calling organization."""

    document_status: Literal["INDEXED", "SUPERSEDED", "FAILED"] = "INDEXED"
    line_id: UUID | None = None
    product_category: str | None = None
    limit: int = 8

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("Retrieval limit must be positive.")


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

    def next_version(self, organization_id: UUID, source_name: str) -> int: ...

    def index(
        self,
        document: KnowledgeDocument,
        parents: Sequence[KnowledgeParentChunk],
        chunks: Sequence[KnowledgeChunk],
    ) -> None: ...

    def record_failure(self, document: KnowledgeDocument) -> None: ...
