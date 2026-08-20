import sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
sys.path.insert(0, str(WEB_BACKEND_SRC))

from odp_api.adapters.retrieval.pgvector import PgVectorRetrievalAdapter
from odp_api.modules.knowledge.ingest import KnowledgeIngestionService
from odp_api.modules.knowledge.models import KnowledgeDocumentStatus
from odp_api.ports.retrieval import RetrievalFilters


def make_two_page_pdf() -> bytes:
    """Create a small, valid PDF with two text paragraphs on each page."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        "<< /Length 94 >>\nstream\nBT /F1 12 Tf 72 720 Td (Page one pressure limit is 20 bar.) Tj 0 -24 Td (Paragraph two requires inspection.) Tj ET\nendstream",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R >>",
        "<< /Length 89 >>\nstream\nBT /F1 12 Tf 72 720 Td (Page two requires protective gloves.) Tj 0 -24 Td (Escalate leaks immediately.) Tj ET\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n{body}\nendobj\n".encode())
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def make_service() -> tuple[KnowledgeIngestionService, PgVectorRetrievalAdapter]:
    index = PgVectorRetrievalAdapter()
    return KnowledgeIngestionService(index, chunk_size_words=4, overlap_words=2), index


def make_docx() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"xml\" ContentType=\"application/xml\"/><Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/></Types>""",
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version=\"1.0\"?><w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p><w:r><w:t>Wear safety gloves.</w:t></w:r></w:p><w:p><w:r><w:t>Record every pressure deviation.</w:t></w:r></w:p></w:body></w:document>""",
        )
    return output.getvalue()


def test_ingests_a_two_page_pdf_with_parent_child_overlap_and_provenance() -> None:
    service, index = make_service()
    organization_id = uuid4()

    document = service.ingest(
        organization_id=organization_id,
        source_name="press rules",
        filename="press-rules.pdf",
        content=make_two_page_pdf(),
    )

    assert document.status == "INDEXED"
    chunks = index.chunks_for_document(document.document_id)
    assert [(chunk.page_number, chunk.paragraph_number) for chunk in chunks] == [
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 1),
        (2, 2),
    ]
    assert all(chunk.parent_chunk_id is not None for chunk in chunks)
    assert chunks[0].text.split()[-2:] == chunks[1].text.split()[:2]


def test_new_source_version_supersedes_the_previous_document() -> None:
    service, index = make_service()
    organization_id = uuid4()

    first = service.ingest(
        organization_id=organization_id,
        source_name="press rules",
        filename="press-rules.pdf",
        content=make_two_page_pdf(),
    )
    second = service.ingest(
        organization_id=organization_id,
        source_name="press rules",
        filename="press-rules-revised.pdf",
        content=make_two_page_pdf(),
    )

    assert index.document(first.document_id).status == "SUPERSEDED"
    assert second.status == "INDEXED"
    assert (first.version, second.version) == (1, 2)
    assert index.document(first.document_id).status == "SUPERSEDED"


def test_ingests_a_valid_docx_and_retains_paragraph_provenance() -> None:
    service, index = make_service()

    document = service.ingest(
        organization_id=uuid4(),
        source_name="glove rules",
        filename="glove-rules.docx",
        content=make_docx(),
    )

    assert document.status == "INDEXED"
    assert [(chunk.page_number, chunk.paragraph_number) for chunk in index.chunks_for_document(document.document_id)] == [
        (1, 1),
        (1, 2),
    ]


def test_search_never_returns_another_tenants_chunk() -> None:
    service, index = make_service()
    organization_id = uuid4()
    other_organization_id = uuid4()
    service.ingest(
        organization_id=organization_id,
        source_name="our rules",
        filename="our-rules.pdf",
        content=make_two_page_pdf(),
    )
    service.ingest(
        organization_id=other_organization_id,
        source_name="other rules",
        filename="other-rules.pdf",
        content=make_two_page_pdf(),
    )

    results = index.search(
        "pressure limit",
        organization_id,
        RetrievalFilters(document_status="INDEXED"),
    )

    assert results
    assert all(result.organization_id == organization_id for result in results)
    assert all(0.0 <= result.vector_score <= 1.0 for result in results)
    assert all(0.0 <= result.bm25_score <= 1.0 for result in results)
    assert all(result.score == pytest.approx((result.vector_score + result.bm25_score) / 2) for result in results)


def test_rejects_unsupported_content_as_a_failed_document() -> None:
    service, _ = make_service()

    document = service.ingest(
        organization_id=uuid4(),
        source_name="not a rule",
        filename="not-a-rule.txt",
        content=b"plain text is not an allowed ingestion format",
    )

    assert document.status == "FAILED"
    assert document.failure_reason == "Unsupported document type: text/plain"
    assert KnowledgeDocumentStatus.__args__ == ("INDEXED", "SUPERSEDED", "FAILED")
