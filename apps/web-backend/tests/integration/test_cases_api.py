from pathlib import Path
import sys
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient


WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.main import create_app
from odp_api.modules.cases.router import InMemoryCaseRepository, create_cases_router
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.inspection.models import DefectCase, InspectionEvent
from odp_schemas.events import InspectionAlert


def test_fixture_case_can_be_listed_with_reproducible_detection_metadata_and_reviewed() -> None:
    """A missing fixture-to-case workflow or transition route makes this fail."""
    app = create_app()
    app.dependency_overrides[get_current_actor] = lambda: Actor(
        UUID("00000000-0000-0000-0000-000000000003"),
        UUID("00000000-0000-0000-0000-000000000001"),
        Role.ADMINISTRATOR,
        frozenset(),
    )
    client = TestClient(app)

    listed = client.get("/api/v1/cases")

    assert listed.status_code == 200
    cases = listed.json()
    assert len(cases) == 1
    case = cases[0]
    assert case["status"] == "PENDING_CONFIRMATION"
    assert case["inspection_events"] == [
        {
            "defect_class": "scratch",
            "confidence": 0.964,
            "model_release": "mock-yolo-1.0",
            "preprocessing_parameters": {"fixture": "scratch-frame-001"},
            "threshold": 0.8,
            "input_frame_sha256": "17a3b51349eb5e1bc31b3c46811941b49c7b2ef32038f9a3ecae48c3c635664a",
        }
    ]

    transitioned = client.post(
        f"/api/v1/cases/{case['case_id']}/transitions",
        json={"status": "IN_REVIEW"},
    )

    assert transitioned.status_code == 200
    assert transitioned.json()["status"] == "IN_REVIEW"


def make_case(organization_id=None) -> DefectCase:
    organization_id = organization_id or uuid4()
    alert = InspectionAlert(
        event_id=uuid4(),
        organization_id=organization_id,
        camera_id=uuid4(),
        occurred_at=datetime.now(UTC),
        defect_class="scratch",
        confidence=0.964,
    )
    event = InspectionEvent.from_alert(
        alert,
        model_release="mock-yolo-1.0",
        preprocessing_parameters=(("fixture", "scratch-frame-001"),),
        threshold=0.80,
        input_frame_sha256="a" * 64,
    )
    return DefectCase(
        case_id=uuid4(), organization_id=organization_id, inspection_events=(event,)
    )


def test_cases_filter_accepts_timezone_less_updated_after_and_returns_newest_first() -> None:
    older_case = make_case()
    newer_case = make_case(older_case.organization_id)
    repository = InMemoryCaseRepository((older_case, newer_case))
    repository.save(newer_case)
    actor = Actor(uuid4(), older_case.organization_id, Role.ADMINISTRATOR, frozenset())
    app = FastAPI()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.include_router(create_cases_router(repository))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/v1/cases?updated_after=2000-01-01T00:00:00")

    assert response.status_code == 200
    assert [case["case_id"] for case in response.json()] == [
        str(newer_case.case_id),
        str(older_case.case_id),
    ]
