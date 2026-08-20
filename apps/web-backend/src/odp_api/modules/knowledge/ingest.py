"""Validated PDF/DOCX extraction and overlapping knowledge-chunk generation."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from odp_api.modules.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeParentChunk,
)
from odp_api.ports.retrieval import KnowledgeIndexPort

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


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
        applicable_line_id: UUID | None = None,
        product_category: str | None = None,
    ) -> KnowledgeDocument:
        """Validate, extract, version, and index one PDF or DOCX document.

        Parser failures intentionally produce a durable ``FAILED`` document rather
        than placing partially extracted chunks into a retrieval provider.
        """
        media_type = content_type or _media_type_for(filename)
        now = datetime.now(UTC)
        document = KnowledgeDocument(
            document_id=uuid4(),
            organization_id=organization_id,
            source_name=source_name,
            filename=filename,
            version=self._index.next_version(organization_id, source_name),
            media_type=media_type,
            content_sha256=hashlib.sha256(content).hexdigest(),
            status="INDEXED",
            indexed_at=now,
            applicable_line_id=applicable_line_id,
            product_category=product_category,
        )
        try:
            paragraphs = _extract_paragraphs(media_type, content)
            parents, chunks = self._chunk(document, paragraphs)
            if not chunks:
                raise ValueError("Document does not contain extractable text.")
        except ValueError as error:
            failed = replace(document, status="FAILED", failure_reason=str(error))
            self._index.record_failure(failed)
            return failed

        self._index.index(document, parents, chunks)
        return document

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
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return _extract_simple_pdf_paragraphs(content)

    try:
        reader = PdfReader(BytesIO(content))
        paragraphs = []
        for page_number, page in enumerate(reader.pages, start=1):
            for paragraph_number, text in enumerate(_split_paragraphs(page.extract_text()), start=1):
                paragraphs.append((page_number, paragraph_number, text))
        return paragraphs
    except Exception as error:
        raise ValueError("Invalid PDF document.") from error


def _extract_simple_pdf_paragraphs(content: bytes) -> list[tuple[int, int, str]]:
    """Fallback for uncompressed text PDFs when an optional parser is absent."""
    objects = {
        int(number): body
        for number, body in re.findall(
            rb"(?m)^(\d+)\s+\d+\s+obj\s*(.*?)\s*endobj", content, re.DOTALL
        )
    }
    pages = [
        body
        for _, body in sorted(objects.items())
        if re.search(rb"/Type\s*/Page\b", body) and not re.search(rb"/Type\s*/Pages\b", body)
    ]
    if not pages:
        raise ValueError("Invalid PDF document.")
    extracted: list[tuple[int, int, str]] = []
    for page_number, page in enumerate(pages, start=1):
        content_ref = re.search(rb"/Contents\s+(\d+)\s+\d+\s+R", page)
        stream_owner = objects.get(int(content_ref.group(1))) if content_ref else page
        if stream_owner is None:
            raise ValueError("Invalid PDF document.")
        stream = re.search(rb"stream\r?\n(.*?)\r?\n?endstream", stream_owner, re.DOTALL)
        if stream is None or b"/Filter" in stream_owner:
            raise ValueError("PDF extraction requires an installed PDF parser.")
        literals = re.findall(rb"\(((?:\\.|[^\\()])*)\)\s*Tj", stream.group(1))
        for paragraph_number, literal in enumerate(literals, start=1):
            text = _decode_pdf_literal(literal).strip()
            if text:
                extracted.append((page_number, paragraph_number, text))
    return extracted


def _decode_pdf_literal(value: bytes) -> str:
    escaped = re.sub(rb"\\([nrtbf()\\])", lambda match: {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}.get(match.group(1), match.group(1)), value)
    return escaped.decode("latin-1")


def _extract_docx_paragraphs(content: bytes) -> list[tuple[int, int, str]]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            if "[Content_Types].xml" not in archive.namelist() or "word/document.xml" not in archive.namelist():
                raise ValueError("Invalid DOCX document.")
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (BadZipFile, ElementTree.ParseError, KeyError, ValueError) as error:
        raise ValueError("Invalid DOCX document.") from error
    paragraphs = []
    for paragraph_number, paragraph in enumerate(root.iter(f"{_WORD_NAMESPACE}p"), start=1):
        text = "".join(paragraph.itertext()).strip()
        if text:
            paragraphs.append((1, paragraph_number, text))
    return paragraphs


def _split_paragraphs(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]
