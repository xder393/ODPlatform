from datetime import UTC, datetime
from pathlib import Path
import sys
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient


WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.modules.notifications.router import (
    InMemoryInspectionAlertRepository,
    create_notifications_router,
)
from odp_api.modules.identity.models import Actor, Role
from odp_schemas.events import InspectionAlert


def test_reconnect_returns_unseen_alert_and_websocket_emits_alert_contract() -> None:
    """A reconnect can reconcile unseen alerts while the socket emits the shared contract."""
    alert = InspectionAlert(
        event_id=uuid4(),
        organization_id=uuid4(),
        camera_id=uuid4(),
        occurred_at=datetime(2026, 8, 19, tzinfo=UTC),
        defect_class="scratch",
        confidence=0.964,
    )
    app = FastAPI()
    actor = Actor(uuid4(), alert.organization_id, Role.ADMINISTRATOR, frozenset())
    app.include_router(
        create_notifications_router(InMemoryInspectionAlertRepository((alert,)), lambda: actor)
    )
    client = TestClient(app)

    reconciled = client.get("/api/v1/inspection-events?updated_after=2026-08-18T00:00:00Z")

    assert reconciled.status_code == 200
    assert reconciled.json() == [alert.model_dump(mode="json")]

    with client.websocket_connect("/ws/inspection-events") as websocket:
        assert websocket.receive_json() == alert.model_dump(mode="json")
