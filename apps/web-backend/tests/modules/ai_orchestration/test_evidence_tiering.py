import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[5] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.adapters.generation.mock import MockLLMAdapter
from odp_api.adapters.retrieval.pgvector import PgVectorRetrievalAdapter
from odp_api.modules.ai_orchestration.service import (
    LOW_CONFIDENCE_ANSWER,
    AdviceService,
)
from odp_api.modules.inspection.models import DefectCase, InspectionEvent
from odp_api.modules.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeParentChunk,
)
from odp_api.ports.generation import (
    GeneratedAdvice,
    GeneratedCitation,
    GenerationRequest,
)
from odp_api.ports.retrieval import RetrievalFilters


def make_case() -> DefectCase:
    organization_id = uuid4()
    line_id = uuid4()
    return DefectCase(
        case_id=uuid4(),
        organization_id=organization_id,
        line_id=line_id,
        product_category="widget",
        inspection_events=(
            InspectionEvent(
                event_id=uuid4(),
                organization_id=organization_id,
                camera_id=uuid4(),
                occurred_at=datetime.now(UTC),
                defect_class="scratch",
                confidence=0.96,
                model_release="mock-yolo-1.0",
                preprocessing_parameters=(),
                threshold=0.8,
                input_frame_sha256="a" * 64,
                line_id=line_id,
            ),
        ),
    )


def add_evidence(
    index: PgVectorRetrievalAdapter,
    case: DefectCase,
    *,
    evidence_kind: str | None,
    line_id: UUID | None,
    product_category: str | None,
    text: str,
    source_name: str | None = None,
) -> KnowledgeChunk:
    document_id = uuid4()
    parent_chunk_id = uuid4()
    document = KnowledgeDocument(
        document_id=document_id,
        organization_id=case.organization_id,
        source_name=source_name or f"{evidence_kind or 'unscoped'} reference",
        filename="reference.pdf",
        version=1,
        media_type="application/pdf",
        content_sha256="a" * 64,
        status="INDEXED",
        indexed_at=datetime.now(UTC),
        evidence_kind=evidence_kind,
        applicable_line_id=line_id,
        product_category=product_category,
    )
    parent = KnowledgeParentChunk(
        parent_chunk_id=parent_chunk_id,
        document_id=document_id,
        organization_id=case.organization_id,
        text=text,
        page_number=1,
        paragraph_number=1,
    )
    child = KnowledgeChunk(
        chunk_id=uuid4(),
        parent_chunk_id=parent_chunk_id,
        document_id=document_id,
        organization_id=case.organization_id,
        text=text,
        page_number=1,
        paragraph_number=1,
        child_index=0,
    )
    index.index(document, (parent,), (child,))
    return child


def test_real_retrieval_strict_scope_excludes_unscoped_and_nonmatching_evidence() -> (
    None
):
    index = PgVectorRetrievalAdapter()
    case = make_case()
    direct = add_evidence(
        index,
        case,
        evidence_kind="CURRENT_SPECIFICATION",
        line_id=case.line_id,
        product_category=case.product_category,
        text="scratch handling current specification",
        source_name="direct current specification",
    )
    add_evidence(
        index,
        case,
        evidence_kind=None,
        line_id=None,
        product_category=None,
        text="scratch unscoped reference",
    )
    add_evidence(
        index,
        case,
        evidence_kind="CURRENT_SPECIFICATION",
        line_id=uuid4(),
        product_category=case.product_category,
        text="scratch other line specification",
        source_name="other line specification",
    )
    add_evidence(
        index,
        case,
        evidence_kind="HISTORICAL_CASE",
        line_id=case.line_id,
        product_category=case.product_category,
        text="scratch same scope historical case",
    )

    results = index.search(
        "scratch",
        case.organization_id,
        RetrievalFilters(
            evidence_kind="CURRENT_SPECIFICATION",
            line_id=case.line_id,
            product_category=case.product_category,
            require_exact_line_scope=True,
            require_exact_product_scope=True,
        ),
    )

    assert [result.chunk_id for result in results] == [direct.chunk_id]
    assert results[0].evidence_kind == "CURRENT_SPECIFICATION"
    assert results[0].applicable_line_id == case.line_id
    assert results[0].product_category == case.product_category


def test_advice_tiers_only_explicitly_applicable_evidence() -> None:
    case = make_case()

    direct_index = PgVectorRetrievalAdapter()
    add_evidence(
        direct_index,
        case,
        evidence_kind=None,
        line_id=None,
        product_category=None,
        text="scratch unscoped reference",
    )
    direct = add_evidence(
        direct_index,
        case,
        evidence_kind="CURRENT_SPECIFICATION",
        line_id=case.line_id,
        product_category=case.product_category,
        text="direct specification",
    )
    high = AdviceService(direct_index, MockLLMAdapter()).advise(case)
    assert high.confidence == "HIGH"
    assert high.citations[0].chunk_id == direct.chunk_id

    historical_index = PgVectorRetrievalAdapter()
    historical = add_evidence(
        historical_index,
        case,
        evidence_kind="HISTORICAL_CASE",
        line_id=case.line_id,
        product_category=case.product_category,
        text="same scope historical case",
    )
    medium = AdviceService(historical_index, MockLLMAdapter()).advise(case)
    assert medium.confidence == "MEDIUM"
    assert medium.citations[0].chunk_id == historical.chunk_id

    cross_product_index = PgVectorRetrievalAdapter()
    cross_product = add_evidence(
        cross_product_index,
        case,
        evidence_kind="CURRENT_SPECIFICATION",
        line_id=uuid4(),
        product_category="gadget",
        text="suspend the line and do not resume production",
    )
    low = AdviceService(cross_product_index, MockLLMAdapter()).advise(case)
    assert low.confidence == "LOW"
    assert low.answer == LOW_CONFIDENCE_ANSWER
    assert low.citations[0].chunk_id == cross_product.chunk_id

    unavailable_index = PgVectorRetrievalAdapter()
    add_evidence(
        unavailable_index,
        case,
        evidence_kind=None,
        line_id=None,
        product_category=None,
        text="unscoped scratch reference",
    )
    unavailable = AdviceService(unavailable_index, MockLLMAdapter()).advise(case)
    assert unavailable.confidence == "UNAVAILABLE"


def test_advice_never_uses_defect_class_as_missing_product_scope() -> None:
    case = replace(make_case(), product_category=None)
    index = PgVectorRetrievalAdapter()
    add_evidence(
        index,
        case,
        evidence_kind="CURRENT_SPECIFICATION",
        line_id=case.line_id,
        product_category="scratch",
        text="scratch specification for an explicitly named product",
    )

    response = AdviceService(index, MockLLMAdapter()).advise(case)

    assert response.confidence == "UNAVAILABLE"


def test_low_tier_never_calls_or_renders_an_untrusted_generator() -> None:
    class UnsafeGenerator:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, request: GenerationRequest) -> GeneratedAdvice:
            self.calls += 1
            return GeneratedAdvice("Suspend the line; do not resume production.", ())

    case = make_case()
    index = PgVectorRetrievalAdapter()
    add_evidence(
        index,
        case,
        evidence_kind="HISTORICAL_CASE",
        line_id=uuid4(),
        product_category="gadget",
        text="cross product reference",
    )
    generator = UnsafeGenerator()

    response = AdviceService(index, generator).advise(case)

    assert response.confidence == "LOW"
    assert response.answer == LOW_CONFIDENCE_ANSWER
    assert generator.calls == 0


def test_orchestration_citations_ignore_generator_fabrication() -> None:
    class FabricatingGenerator:
        def generate(self, request: GenerationRequest) -> GeneratedAdvice:
            return GeneratedAdvice(
                "Follow retrieved evidence.",
                (
                    GeneratedCitation(
                        source_name="fabricated source",
                        document_version=999,
                        page_number=99,
                        paragraph_number=99,
                        snippet="fabricated snippet",
                    ),
                ),
            )

    case = make_case()
    index = PgVectorRetrievalAdapter()
    chunk = add_evidence(
        index,
        case,
        evidence_kind="CURRENT_SPECIFICATION",
        line_id=case.line_id,
        product_category=case.product_category,
        text="trusted evidence",
    )

    response = AdviceService(index, FabricatingGenerator()).advise(case)

    assert response.confidence == "HIGH"
    assert response.model_dump()["citations"] == [
        {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "source_name": "CURRENT_SPECIFICATION reference",
            "document_version": 1,
            "page_number": 1,
            "paragraph_number": 1,
            "snippet": "trusted evidence",
        }
    ]
