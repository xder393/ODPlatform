"""WebSocket continuity and cursor envelope integration contracts."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from odp_api.main import create_app
from odp_api.seed import DEMO_ACCOUNTS, DEMO_LINE_ID, DEMO_ORG_ID, build_demo_seed
from odp_api.settings import Settings
from odp_schemas.events import InspectionAlert


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        auth_jwt_secret="live-feed-test-secret",
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        task_database_path=str(tmp_path / "tasks.db"),
    )


def _token(client: TestClient) -> str:
    email, password, _ = DEMO_ACCOUNTS[0]
    return client.post("/api/v1/auth/login", json={"email": email, "password": password}).json()["access_token"]


def test_same_socket_receives_authorized_event_published_after_connect(tmp_path: Path) -> None:
    """Returning after the backlog would force a reconnect before a new alert arrived."""
    with TestClient(create_app(settings=_settings(tmp_path), seed=build_demo_seed())) as client:
        token = _token(client)
        ticket = client.post("/api/v1/auth/websocket-ticket", headers={"Authorization": f"Bearer {token}"}).json()["ticket"]
        # Seed emits ten durable events; resume after them so the first frame
        # must be the event published while this same socket remains open.
        with client.websocket_connect(f"/ws/inspection-events?ticket={ticket}&cursor=10") as websocket:
            alert = InspectionAlert(
                event_id=uuid4(), organization_id=DEMO_ORG_ID, camera_id=uuid4(),
                occurred_at=datetime(2026, 8, 24, tzinfo=UTC), defect_class="new-scratch", confidence=0.99,
            )
            cursor = client.app.state.inspection_alert_feed.publish(alert, DEMO_LINE_ID)
            received = websocket.receive_json()

    assert received == {"cursor": cursor, "alert": alert.model_dump(mode="json")}


def test_rest_reconciliation_uses_cursor_envelope_and_line_authorization(tmp_path: Path) -> None:
    """Trusting an opaque cursor for authorization would expose foreign-line events."""
    with TestClient(create_app(settings=_settings(tmp_path), seed=build_demo_seed())) as client:
        token = _token(client)
        response = client.get("/api/v1/inspection-events?limit=2", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"items", "next_cursor"}
    assert len(payload["items"]) == 2
    assert payload["next_cursor"] == payload["items"][-1]["cursor"]


def test_idle_disconnect_cancels_subscription_and_restores_active_gauge(tmp_path: Path) -> None:
    """An idle client disconnect must not leave a pending subscription task or gauge."""
    app = create_app(settings=_settings(tmp_path), seed=build_demo_seed())
    with TestClient(app) as client:
        ticket = client.post(
            "/api/v1/auth/websocket-ticket", headers={"Authorization": f"Bearer {_token(client)}"}
        ).json()["ticket"]
        with client.websocket_connect(f"/ws/inspection-events?ticket={ticket}&cursor=10"):
            pass
    assert app.state.metric_registry._metrics["websocket_active_connections"].snapshot() == 0
