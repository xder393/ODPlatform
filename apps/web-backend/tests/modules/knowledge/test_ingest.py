import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
sys.path.insert(0, str(WEB_BACKEND_SRC))

from odp_api.adapters.retrieval.pgvector import (
    PGVECTOR_EMBEDDING_DIMENSIONS,
    PgVectorPostgresAdapter,
    PgVectorRetrievalAdapter,
)
from odp_api.modules.knowledge.ingest import KnowledgeIngestionService
from odp_api.modules.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
    KnowledgeParentChunk,
)
from odp_api.ports.retrieval import RetrievalFilters


def make_two_page_pdf() -> bytes:
    """Create a realistic filtered PDF that requires a full PDF parser."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page_texts = (
        b"BT /F1 12 Tf 72 720 Td (Page one pressure limit is 20 bar.) Tj 0 -24 Td (Paragraph two requires inspection.) Tj ET",
        b"BT /F1 12 Tf 72 720 Td (Page two requires protective gloves.) Tj 0 -24 Td (Escalate leaks immediately.) Tj ET",
    )
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(text)
        page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def make_service() -> tuple[KnowledgeIngestionService, PgVectorRetrievalAdapter]:
    index = PgVectorRetrievalAdapter()
    return KnowledgeIngestionService(index, chunk_size_words=4, overlap_words=2), index


def make_docx(
    *,
    include_package_relationship: bool = True,
    office_document_target_mode: str | None = None,
) -> bytes:
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
        if include_package_relationship:
            target_mode = (
                f' TargetMode="{office_document_target_mode}"'
                if office_document_target_mode is not None
                else ""
            )
            archive.writestr(
                "_rels/.rels",
                f"""<?xml version=\"1.0\"?><Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"{target_mode}/></Relationships>""",
            )
    return output.getvalue()


def valid_embedding() -> list[float]:
    return [0.25] * PGVECTOR_EMBEDDING_DIMENSIONS


class RecordingPostgresExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def fetch_all(self, sql: str, parameters: dict[str, object]) -> list[dict[str, object]]:
        self.calls.append((sql, parameters))
        return [
            {
                "chunk_id": str(uuid4()),
                "parent_chunk_id": str(uuid4()),
                "document_id": str(uuid4()),
                "organization_id": str(parameters["organization_id"]),
                "document_version": 2,
                "source_name": "press rules",
                "text": "Pressure limit is 20 bar.",
                "page_number": 1,
                "paragraph_number": 1,
                "vector_score": 0.75,
                "bm25_score": 1.0,
                "combined_score": 0.875,
                "evidence_kind": "CURRENT_SPECIFICATION",
                "applicable_line_id": parameters["line_id"],
                "product_category": parameters["product_category"],
            }
        ]

    def execute(self, sql: str, parameters: dict[str, object]) -> None:
        self.calls.append((sql, parameters))


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


def test_records_a_failed_document_when_pypdf_rejects_malformed_pdf() -> None:
    service, _ = make_service()

    document = service.ingest(
        organization_id=uuid4(),
        source_name="corrupt rules",
        filename="corrupt-rules.pdf",
        content=b"%PDF-1.7\nthis is not a PDF object graph\n%%EOF",
    )

    assert document.status == "FAILED"
    assert document.failure_reason == "Invalid PDF document."


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


def test_rejects_docx_zip_without_office_document_relationship() -> None:
    service, _ = make_service()

    document = service.ingest(
        organization_id=uuid4(),
        source_name="spoofed rules",
        filename="spoofed-rules.docx",
        content=make_docx(include_package_relationship=False),
    )

    assert document.status == "FAILED"
    assert document.failure_reason == "Invalid DOCX document."


def test_rejects_docx_with_external_office_document_relationship() -> None:
    service, _ = make_service()

    document = service.ingest(
        organization_id=uuid4(),
        source_name="external rules",
        filename="external-rules.docx",
        content=make_docx(office_document_target_mode="External"),
    )

    assert document.status == "FAILED"
    assert document.failure_reason == "Invalid DOCX document."


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


def test_postgres_retrieval_executes_tenant_scoped_normalized_hybrid_query() -> None:
    executor = RecordingPostgresExecutor()
    adapter = PgVectorPostgresAdapter(executor, embed=lambda _: valid_embedding())
    organization_id = uuid4()
    line_id = uuid4()

    results = adapter.search(
        "pressure limit",
        organization_id,
        RetrievalFilters(
            evidence_kind="CURRENT_SPECIFICATION",
            line_id=line_id,
            product_category="widget",
            require_exact_line_scope=True,
            require_exact_product_scope=True,
            limit=3,
        ),
    )

    assert [result.score for result in results] == [0.875]
    sql, parameters = executor.calls[-1]
    assert "WHERE c.organization_id = %(organization_id)s" in sql
    assert "d.evidence_kind = %(evidence_kind)s" in sql
    assert "NOT %(require_exact_line_scope)s" in sql
    assert "NOT %(require_exact_product_scope)s" in sql
    assert "vector_score" in sql and "bm25_score" in sql and "combined_score" in sql
    assert parameters == {
        "query": "pressure limit",
        "query_embedding": "[" + ",".join(["0.25"] * PGVECTOR_EMBEDDING_DIMENSIONS) + "]",
        "organization_id": organization_id,
        "document_status": "INDEXED",
        "evidence_kind": "CURRENT_SPECIFICATION",
        "require_evidence_kind": False,
        "line_id": line_id,
        "require_exact_line_scope": True,
        "product_category": "widget",
        "require_exact_product_scope": True,
        "exclude_product_category": None,
        "limit": 3,
    }


def test_postgres_adapter_rejects_wrong_embedding_dimension_before_database_execution() -> None:
    executor = RecordingPostgresExecutor()
    adapter = PgVectorPostgresAdapter(executor, embed=lambda _: [0.25, -0.25])

    with pytest.raises(ValueError, match="64 dimensions"):
        adapter.search("pressure limit", uuid4(), RetrievalFilters())

    assert executor.calls == []


def test_postgres_index_persists_tenant_scoped_document_parent_and_child_rows() -> None:
    executor = RecordingPostgresExecutor()
    adapter = PgVectorPostgresAdapter(executor, embed=lambda _: valid_embedding())
    organization_id = uuid4()
    document_id = uuid4()
    parent_id = uuid4()
    document = KnowledgeDocument(
        document_id=document_id,
        organization_id=organization_id,
        source_name="press rules",
        filename="press-rules.pdf",
        version=1,
        media_type="application/pdf",
        content_sha256="a" * 64,
        status="INDEXED",
        indexed_at=datetime.now(UTC),
    )
    parent = KnowledgeParentChunk(
        parent_chunk_id=parent_id,
        document_id=document_id,
        organization_id=organization_id,
        text="Pressure limit is 20 bar.",
        page_number=1,
        paragraph_number=1,
    )
    child = KnowledgeChunk(
        chunk_id=uuid4(),
        parent_chunk_id=parent_id,
        document_id=document_id,
        organization_id=organization_id,
        text="Pressure limit is 20 bar.",
        page_number=1,
        paragraph_number=1,
        child_index=0,
    )

    adapter.index(document, [parent], [child])

    assert len(executor.calls) == 4
    assert "UPDATE knowledge_documents" in executor.calls[0][0]
    assert "INSERT INTO knowledge_documents" in executor.calls[1][0]
    assert "INSERT INTO knowledge_parent_chunks" in executor.calls[2][0]
    assert "INSERT INTO knowledge_chunk_index" in executor.calls[3][0]
    assert all(parameters["organization_id"] == organization_id for _, parameters in executor.calls)
    assert executor.calls[-1][1]["embedding"] == "[" + ",".join(["0.25"] * PGVECTOR_EMBEDDING_DIMENSIONS) + "]"

    rejected_executor = RecordingPostgresExecutor()
    rejected_adapter = PgVectorPostgresAdapter(
        rejected_executor, embed=lambda _: [0.25, -0.25]
    )
    with pytest.raises(ValueError, match="64 dimensions"):
        rejected_adapter.index(document, [parent], [child])
    assert rejected_executor.calls == []
