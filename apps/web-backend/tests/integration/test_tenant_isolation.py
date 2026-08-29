import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_schemas.events import InspectionAlert

from odp_api.modules.cases.router import InMemoryCaseRepository, create_cases_router
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import (
    InMemoryPasswordVerifier,
    InMemoryReauthenticationStore,
    ReauthenticationService,
    create_auth_router,
    get_current_actor,
)
from odp_api.modules.inspection.models import DefectCase, InspectionEvent
from odp_api.modules.notifications.router import (
    InMemoryInspectionAlertRepository,
    create_notifications_router,
)


def make_case(organization_id, line_id) -> DefectCase:
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
        preprocessing_parameters=(),
        threshold=0.80,
        input_frame_sha256="a" * 64,
        line_id=line_id,
    )
    return DefectCase(
        case_id=uuid4(),
        organization_id=organization_id,
        inspection_events=(event,),
        line_id=line_id,
    )


def test_case_and_alert_queries_are_tenant_scoped() -> None:
    organization_id = uuid4()
    another_organization_id = uuid4()
    line_id = uuid4()
    actor = Actor(uuid4(), organization_id, Role.INSPECTOR, frozenset({line_id}))
    own_case = make_case(organization_id, line_id)
    other_case = make_case(another_organization_id, uuid4())
    app = FastAPI()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.include_router(
        create_cases_router(InMemoryCaseRepository((own_case, other_case)))
    )
    events = tuple(
        event for case in (own_case, other_case) for event in case.inspection_events
    )
    app.include_router(
        create_notifications_router(
            InMemoryInspectionAlertRepository(
                tuple(event.to_alert() for event in events),
                {event.event_id: event.line_id for event in events},
            ),
        )
    )
    client = TestClient(app)

    cases = client.get("/api/v1/cases")
    alerts = client.get("/api/v1/inspection-events")

    assert cases.status_code == 200
    assert [item["case_id"] for item in cases.json()] == [str(own_case.case_id)]
    assert alerts.status_code == 200
    assert [item["alert"]["organization_id"] for item in alerts.json()["items"]] == [
        str(organization_id)
    ]


def test_simulated_pause_requires_recent_password_reauthentication() -> None:
    organization_id = uuid4()
    line_id = uuid4()
    actor = Actor(uuid4(), organization_id, Role.INSPECTOR, frozenset({line_id}))
    case = make_case(organization_id, line_id)
    reauth = ReauthenticationService(InMemoryReauthenticationStore())
    app = FastAPI()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.include_router(
        create_cases_router(
            InMemoryCaseRepository((case,)),
            reauthentication_service=reauth,
        )
    )
    app.include_router(
        create_auth_router(
            reauthentication_service=reauth,
            password_verifier=InMemoryPasswordVerifier(
                {actor.actor_id: "test-password"}
            ),
        )
    )
    client = TestClient(app)

    rejected = client.post(f"/api/v1/cases/{case.case_id}/pause")
    reauthenticated = client.post(
        "/api/v1/auth/reauthenticate", json={"password": "test-password"}
    )
    accepted = client.post(f"/api/v1/cases/{case.case_id}/pause")

    assert rejected.status_code == 403
    assert reauthenticated.status_code == 200
    assert accepted.status_code == 200
    assert accepted.json() == {
        "status": "SIMULATED",
        "message": "No production line was paused.",
    }
