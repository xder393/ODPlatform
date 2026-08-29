import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[5] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_schemas.events import InspectionAlert

from odp_api.adapters.generation.mock import MockLLMAdapter
from odp_api.main import create_app
from odp_api.modules.ai_orchestration.router import create_advice_router
from odp_api.modules.ai_orchestration.service import (
    HUMAN_REVIEW_MESSAGE,
    AdviceService,
)
from odp_api.modules.cases.router import InMemoryCaseRepository
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.inspection.models import DefectCase, InspectionEvent
from odp_api.ports.generation import GeneratedAdvice, GenerationRequest
from odp_api.ports.retrieval import RetrievalFilters, RetrievedChunk

GOLDEN_DATASET = json.loads(
    (Path(__file__).parents[2] / "golden" / "rag_advice.json").read_text()
)


def make_case() -> DefectCase:
    organization_id = uuid4()
    line_id = uuid4()
    event = InspectionEvent.from_alert(
        InspectionAlert(
            event_id=uuid4(),
            organization_id=organization_id,
            camera_id=uuid4(),
            occurred_at=datetime.now(UTC),
            defect_class="scratch",
            confidence=0.96,
        ),
        model_release="mock-yolo-1.0",
        preprocessing_parameters=(),
        threshold=0.8,
        input_frame_sha256="a" * 64,
        line_id=line_id,
    )
    return DefectCase(
        case_id=uuid4(),
        organization_id=organization_id,
        inspection_events=(event,),
        line_id=line_id,
        product_category="widget",
    )


def chunk(case: DefectCase, record: dict[str, object]) -> RetrievedChunk:
    scope = record["scope"]
    return RetrievedChunk(
        chunk_id=uuid4(),
        parent_chunk_id=uuid4(),
        document_id=uuid4(),
        organization_id=case.organization_id,
        document_version=int(record["document_version"]),
        source_name=str(record["source_name"]),
        text=str(record["snippet"]),
        page_number=int(record["page_number"]),
        paragraph_number=int(record["paragraph_number"]),
        vector_score=1.0,
        bm25_score=1.0,
        score=1.0,
        evidence_kind=(
            "HISTORICAL_CASE" if scope == "same_line" else "CURRENT_SPECIFICATION"
        ),
        applicable_line_id=case.line_id if scope != "cross_product" else uuid4(),
        product_category="widget" if scope != "cross_product" else "gadget",
    )


class GoldenRetrieval:
    def __init__(self, case: DefectCase, record: dict[str, object]) -> None:
        self.case = case
        self.record = record
        self.calls: list[RetrievalFilters] = []
        self.result = None if record["scope"] == "unavailable" else chunk(case, record)

    def search(
        self, query: str, organization_id: UUID, filters: RetrievalFilters
    ) -> list[RetrievedChunk]:
        self.calls.append(filters)
        assert query == "scratch inspection guidance"
        assert organization_id == self.case.organization_id
        scope = self.record["scope"]
        if (
            scope == "direct"
            and filters.evidence_kind == "CURRENT_SPECIFICATION"
            and filters.line_id == self.case.line_id
            and filters.product_category == "widget"
            and filters.require_exact_line_scope
            and filters.require_exact_product_scope
        ):
            return [self.result]
        if (
            scope == "same_line"
            and filters.evidence_kind == "HISTORICAL_CASE"
            and filters.line_id == self.case.line_id
            and filters.product_category == "widget"
            and filters.require_exact_line_scope
            and filters.require_exact_product_scope
        ):
            return [self.result]
        if (
            scope == "cross_product"
            and filters.require_evidence_kind
            and filters.exclude_product_category == "widget"
        ):
            return [self.result]
        return []


def test_golden_dataset_assigns_confidence_and_source_provenance_deterministically() -> None:
    for record in GOLDEN_DATASET:
        case = make_case()
        advice_service = AdviceService(GoldenRetrieval(case, record), MockLLMAdapter())

        first = advice_service.advise(case)
        second = advice_service.advise(case)

        assert first == second
        assert first.confidence == record["confidence"]
        if first.confidence == "UNAVAILABLE":
            assert first.answer == HUMAN_REVIEW_MESSAGE
            assert first.citations == []
            continue
        assert first.citations[0].source_name == record["source_name"]
        assert first.citations[0].document_version == record["document_version"]
        assert first.citations[0].page_number == record["page_number"]
        assert first.citations[0].paragraph_number == record["paragraph_number"]
        assert first.citations[0].snippet == record["snippet"]


def test_low_confidence_advice_never_recommends_pausing_a_line() -> None:
    case = make_case()
    record = next(record for record in GOLDEN_DATASET if record["scope"] == "cross_product")

    response = AdviceService(GoldenRetrieval(case, record), MockLLMAdapter()).advise(case)

    assert response.confidence == "LOW"
    assert "pause" not in response.answer.lower()


def test_low_confidence_advice_removes_any_line_pause_recommendation_from_generator() -> None:
    class UnsafeGenerator:
        def generate(self, request: GenerationRequest) -> GeneratedAdvice:
            return GeneratedAdvice("Pause the production line immediately.", ())

    case = make_case()
    record = next(record for record in GOLDEN_DATASET if record["scope"] == "cross_product")

    response = AdviceService(GoldenRetrieval(case, record), UnsafeGenerator()).advise(case)

    assert response.confidence == "LOW"
    assert "pause" not in response.answer.lower()
    assert "stop" not in response.answer.lower()


def test_advice_endpoint_enforces_authenticated_tenant_scope() -> None:
    case = make_case()
    record = next(record for record in GOLDEN_DATASET if record["scope"] == "direct")
    app = FastAPI()
    own_actor = Actor(uuid4(), case.organization_id, Role.ADMINISTRATOR, frozenset())
    app.dependency_overrides[get_current_actor] = lambda: own_actor
    app.include_router(
        create_advice_router(
            InMemoryCaseRepository((case,)),
            AdviceService(GoldenRetrieval(case, record), MockLLMAdapter()),
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    allowed = client.post(f"/api/v1/cases/{case.case_id}/advice")
    assert allowed.status_code == 200
    assert allowed.json()["confidence"] == "HIGH"

    foreign_actor = Actor(uuid4(), uuid4(), Role.ADMINISTRATOR, frozenset())
    app.dependency_overrides[get_current_actor] = lambda: foreign_actor
    denied = client.post(f"/api/v1/cases/{case.case_id}/advice")
    assert denied.status_code == 404


def test_application_factory_exposes_the_exact_advice_endpoint() -> None:
    app = create_app()
    app.dependency_overrides[get_current_actor] = lambda: Actor(
        uuid4(),
        UUID("00000000-0000-0000-0000-000000000001"),
        Role.ADMINISTRATOR,
        frozenset(),
    )
    client = TestClient(app)
    case_id = client.get("/api/v1/cases").json()[0]["case_id"]

    response = client.post(f"/api/v1/cases/{case_id}/advice")

    assert response.status_code == 200
    assert response.json() == {
        "answer": HUMAN_REVIEW_MESSAGE,
        "citations": [],
        "confidence": "UNAVAILABLE",
    }
