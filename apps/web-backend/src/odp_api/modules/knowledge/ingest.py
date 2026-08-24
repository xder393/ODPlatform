"""Validated PDF/DOCX extraction and overlapping knowledge-chunk generation."""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from odp_api.modules.knowledge.models import (
    EvidenceKind,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeParentChunk,
)
from odp_api.ports.retrieval import KnowledgeIndexPort
from pypdf import PdfReader

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CONTENT_TYPES_NAMESPACE = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_PACKAGE_RELATIONSHIPS_NAMESPACE = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_DOCUMENT_RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
_WORD_DOCUMENT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


class KnowledgeIngestionService:
    """Turns an approved document upload into source-addressable index rows."""

    def __init__(
        self,
        index: KnowledgeIndexPort,
        *,
        chunk_size_words: int = 180,
        overlap_words: int = 40,
    ) -> None:
        if chunk_size_words < 1:
            raise ValueError("Chunk size must be positive.")
        if overlap_words < 0 or overlap_words >= chunk_size_words:
            raise ValueError("Chunk overlap must be non-negative and smaller than chunk size.")
        self._index = index
        self._chunk_size_words = chunk_size_words
        self._overlap_words = overlap_words

    def ingest(
        self,
        *,
        organization_id: UUID,
        source_name: str,
        filename: str,
        content: bytes,
        content_type: str | None = None,
        evidence_kind: EvidenceKind | None = None,
        applicable_line_id: UUID | None = None,
        product_category: str | None = None,
    ) -> KnowledgeDocument:
        """Validate, extract, version, and index one PDF or DOCX document.

        Parser failures intentionally produce a durable ``FAILED`` document rather
        than placing partially extracted chunks into a retrieval provider.
        """
        content_sha256 = hashlib.sha256(_normalised_raw_content(content)).hexdigest()
        existing = self._index.find_indexed_document(
            organization_id, source_name, content_sha256
        )
        if existing is not None:
            return existing

        media_type = content_type or _media_type_for(filename)
        now = datetime.now(UTC)
        document = KnowledgeDocument(
            document_id=uuid4(),
            organization_id=organization_id,
            source_name=source_name,
            filename=filename,
            # The storage adapter allocates this inside its source lock.  The
            # UUID is safe to allocate here because chunks reference it.
            version=0,
            media_type=media_type,
            content_sha256=content_sha256,
            status="INDEXED",
            indexed_at=now,
            evidence_kind=evidence_kind,
            applicable_line_id=applicable_line_id,
            product_category=product_category,
        )
        try:
            paragraphs = _extract_paragraphs(media_type, content)
            parents, chunks = self._chunk(document, paragraphs)
            if not chunks:
                raise ValueError("Document does not contain extractable text.")
        except ValueError as error:
            return self._index.record_failure_atomically(
                replace(document, status="FAILED", failure_reason=str(error))
            )

        return self._index.index_atomically(document, parents, chunks)

    def _chunk(
        self,
        document: KnowledgeDocument,
        paragraphs: Iterable[tuple[int, int, str]],
    ) -> tuple[list[KnowledgeParentChunk], list[KnowledgeChunk]]:
        parents: list[KnowledgeParentChunk] = []
        children: list[KnowledgeChunk] = []
        for page_number, paragraph_number, text in paragraphs:
            words = text.split()
            if not words:
                continue
            parent = KnowledgeParentChunk(
                parent_chunk_id=uuid4(),
                document_id=document.document_id,
                organization_id=document.organization_id,
                text=" ".join(words),
                page_number=page_number,
                paragraph_number=paragraph_number,
            )
            parents.append(parent)
            step = self._chunk_size_words - self._overlap_words
            for child_index, start in enumerate(range(0, len(words), step)):
                child_words = words[start : start + self._chunk_size_words]
                if not child_words:
                    break
                children.append(
                    KnowledgeChunk(
                        chunk_id=uuid4(),
                        parent_chunk_id=parent.parent_chunk_id,
                        document_id=document.document_id,
                        organization_id=document.organization_id,
                        text=" ".join(child_words),
                        page_number=page_number,
                        paragraph_number=paragraph_number,
                        child_index=child_index,
                    )
                )
                if start + self._chunk_size_words >= len(words):
                    break
        return parents, children


def _normalised_raw_content(content: bytes) -> bytes:
    """Canonicalize line endings before hashing original upload bytes.

    The hash deliberately precedes PDF/DOCX extraction: parser output can vary
    across library versions, while normalized upload bytes are stable and keep
    source revisions auditable.
    """
    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _media_type_for(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return PDF_MEDIA_TYPE
    if suffix == ".docx":
        return DOCX_MEDIA_TYPE
    if suffix == ".doc":
        return "application/msword"
    guessed, _ = mimetypes.guess_type(filename)
    if guessed is not None:
        return guessed
    if suffix:
        return f"text/{suffix.removeprefix('.')}"
    return "application/octet-stream"


def _extract_paragraphs(media_type: str, content: bytes) -> list[tuple[int, int, str]]:
    if media_type == PDF_MEDIA_TYPE:
        return _extract_pdf_paragraphs(content)
    if media_type == DOCX_MEDIA_TYPE:
        return _extract_docx_paragraphs(content)
    raise ValueError(f"Unsupported document type: {media_type}")


def _extract_pdf_paragraphs(content: bytes) -> list[tuple[int, int, str]]:
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content:
        raise ValueError("Invalid PDF document.")
    try:
        reader = PdfReader(BytesIO(content))
        paragraphs = []
        for page_number, page in enumerate(reader.pages, start=1):
            for paragraph_number, text in enumerate(_split_paragraphs(page.extract_text()), start=1):
                paragraphs.append((page_number, paragraph_number, text))
        return paragraphs
    except Exception as error:
        raise ValueError("Invalid PDF document.") from error


def _extract_docx_paragraphs(content: bytes) -> list[tuple[int, int, str]]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            if {
                "[Content_Types].xml",
                "_rels/.rels",
                "word/document.xml",
            }.difference(archive.namelist()):
                raise ValueError("Invalid DOCX document.")
            content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
            relationships = ElementTree.fromstring(archive.read("_rels/.rels"))
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (BadZipFile, ElementTree.ParseError, KeyError, ValueError) as error:
        raise ValueError("Invalid DOCX document.") from error
    if content_types.tag != f"{_CONTENT_TYPES_NAMESPACE}Types" or relationships.tag != f"{_PACKAGE_RELATIONSHIPS_NAMESPACE}Relationships":
        raise ValueError("Invalid DOCX document.")
    has_word_document_content_type = any(
        override.get("PartName") == "/word/document.xml"
        and override.get("ContentType") == _WORD_DOCUMENT_CONTENT_TYPE
        for override in content_types.iter(f"{_CONTENT_TYPES_NAMESPACE}Override")
    )
    has_office_document_relationship = any(
        relationship.get("Type") == _OFFICE_DOCUMENT_RELATIONSHIP
        and relationship.get("Target", "").lstrip("/") == "word/document.xml"
        and relationship.get("TargetMode") in {None, "Internal"}
        for relationship in relationships.iter(f"{_PACKAGE_RELATIONSHIPS_NAMESPACE}Relationship")
    )
    if not has_word_document_content_type or not has_office_document_relationship:
        raise ValueError("Invalid DOCX document.")
    if root.tag != f"{_WORD_NAMESPACE}document" or root.find(f"{_WORD_NAMESPACE}body") is None:
        raise ValueError("Invalid DOCX document.")
    paragraphs = []
    for paragraph_number, paragraph in enumerate(root.iter(f"{_WORD_NAMESPACE}p"), start=1):
        text = "".join(paragraph.itertext()).strip()
        if text:
            paragraphs.append((1, paragraph_number, text))
    return paragraphs


def _split_paragraphs(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]
