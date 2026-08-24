"""Versioned knowledge-document and source-provenance records."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

KnowledgeDocumentStatus = Literal["INDEXED", "SUPERSEDED", "FAILED"]
EvidenceKind = Literal["CURRENT_SPECIFICATION", "HISTORICAL_CASE"]
EVIDENCE_KINDS: frozenset[EvidenceKind] = frozenset(
    {"CURRENT_SPECIFICATION", "HISTORICAL_CASE"}
)


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """A durable document version, retained even after it is superseded."""

    document_id: UUID
    organization_id: UUID
    source_name: str
    filename: str
    version: int
    media_type: str
    content_sha256: str
    status: KnowledgeDocumentStatus
    indexed_at: datetime
    evidence_kind: EvidenceKind | None = None
    applicable_line_id: UUID | None = None
    product_category: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.evidence_kind is not None and self.evidence_kind not in EVIDENCE_KINDS:
            raise ValueError(f"Unsupported evidence kind: {self.evidence_kind}")


@dataclass(frozen=True, slots=True)
class KnowledgeParentChunk:
    """The complete source paragraph from which overlapping children are made."""

    parent_chunk_id: UUID
    document_id: UUID
    organization_id: UUID
    text: str
    page_number: int
    paragraph_number: int


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    """A child retrieval window retaining its parent and source location."""

    chunk_id: UUID
    parent_chunk_id: UUID
    document_id: UUID
    organization_id: UUID
    text: str
    page_number: int
    paragraph_number: int
    child_index: int
