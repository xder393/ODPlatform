"""P1C operational API contracts: sessions, diagnostics, replay, evidence."""

from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from odp_api.main import create_app
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import get_current_actor
from odp_api.settings import Settings

ORG_ID = UUID("10000000-0000-4000-8000-000000000001")
LINE_ID = UUID("20000000-0000-4000-8000-000000000001")


def _client(tmp_path: Path, role: Role) -> TestClient:
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'runtime.db'}"))
    app.dependency_overrides[get_current_actor] = lambda: Actor(
        uuid4(), ORG_ID, role, frozenset({LINE_ID})
    )
    return TestClient(app)


def test_supervisor_can_create_and_stop_an_idempotent_inspection_session(tmp_path: Path) -> None:
    client = _client(tmp_path, Role.SUPERVISOR)
    payload = {
        "camera_id": str(uuid4()),
        "line_id": str(LINE_ID),
        "source_type": "RECORDED",
        "source_ref": "scratch-loop",
        "secret_ref": "secret/camera-1",
    }
    headers = {"Idempotency-Key": "session-demo-1"}

    created = client.post("/api/v1/inspection-sessions", json=payload, headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "START_REQUESTED"
    assert body["sanitized_uri"] == "scratch-loop"
    assert "password" not in body["sanitized_uri"]

    replay = client.post("/api/v1/inspection-sessions", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["session_id"] == body["session_id"]

    stopped = client.post(f"/api/v1/inspection-sessions/{body['session_id']}:stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "STOP_REQUESTED"


def test_inspector_cannot_start_an_inspection_session(tmp_path: Path) -> None:
    client = _client(tmp_path, Role.INSPECTOR)
    response = client.post(
        "/api/v1/inspection-sessions",
        headers={"Idempotency-Key": "inspector-forbidden"},
        json={
            "camera_id": str(uuid4()),
            "line_id": str(LINE_ID),
            "source_type": "RECORDED",
            "source_ref": "scratch-loop",
        },
    )
    assert response.status_code == 403


def test_task_diagnostics_and_evidence_never_cross_tenant_boundaries(tmp_path: Path) -> None:
    client = _client(tmp_path, Role.INSPECTOR)
    tasks = client.get("/api/v1/inference-tasks")
    assert tasks.status_code == 200
    assert tasks.json()["items"] == []

    unknown = uuid4()
    assert client.get(f"/api/v1/inference-tasks/{unknown}").status_code == 404
    assert client.get(f"/api/v1/artifacts/{unknown}/evidence-url").status_code == 404


def test_authenticated_actor_profile_exposes_scope_without_credentials(tmp_path: Path) -> None:
    client = _client(tmp_path, Role.INSPECTOR)
    profile = client.get("/api/v1/auth/me")
    assert profile.status_code == 200
    assert profile.json()["organization_id"] == str(ORG_ID)
    assert profile.json()["line_ids"] == [str(LINE_ID)]
    assert "password" not in profile.json()
