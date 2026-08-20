import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.adapters.notifications.redis_stream import RedisStreamInspectionAlertFeed
from odp_api.adapters.tasks.redis_stream import RedisStreamTaskQueue
from odp_api.main import create_app
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.notifications.router import (
    InMemoryInspectionAlertRepository,
    create_notifications_router,
)

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
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.include_router(create_notifications_router(InMemoryInspectionAlertRepository((alert,))))
    client = TestClient(app)

    reconciled = client.get("/api/v1/inspection-events?updated_after=2026-08-18T00:00:00Z")

    assert reconciled.status_code == 200
    assert reconciled.json() == [alert.model_dump(mode="json")]

    with client.websocket_connect("/ws/inspection-events") as websocket:
        assert websocket.receive_json() == alert.model_dump(mode="json")


def test_redis_stream_notification_feed_preserves_the_existing_reconciliation_contract() -> None:
    """A Redis-backed feed must return the same authorized alert data as the local feed."""
    class FakeRedisStream:
        def __init__(self) -> None:
            self.entries: list[tuple[str, dict[str, str]]] = []

        def xadd(self, stream: str, fields: dict[str, str]) -> str:
            self.entries.append((stream, fields))
            return "1-0"

        def xrange(self, stream: str) -> list[tuple[str, dict[str, str]]]:
            return [entry for entry in self.entries if entry[0] == stream]

    alert = InspectionAlert(
        event_id=uuid4(),
        organization_id=uuid4(),
        camera_id=uuid4(),
        occurred_at=datetime(2026, 8, 19, tzinfo=UTC),
        defect_class="scratch",
        confidence=0.964,
    )
    redis = FakeRedisStream()
    feed = RedisStreamInspectionAlertFeed(redis)

    feed.publish(alert, line_id=None)

    stored = feed.list(alert.organization_id, datetime(2026, 8, 18, tzinfo=UTC))
    assert [item.alert for item in stored] == [alert]


def test_redis_stream_notification_feed_accepts_redis_byte_fields() -> None:
    """A real redis-py consumer returns bytes, not the string fields used by the fake."""
    class ByteRedisStream:
        def xadd(self, stream: str, fields: dict[str, str]) -> str:
            return "1-0"

        def xrange(self, stream: str) -> list[tuple[str, dict[bytes, bytes]]]:
            return [
                (
                    "1-0",
                    {
                        b"alert": (
                            b'{"event_id":"00000000-0000-0000-0000-000000000001",'
                            b'"organization_id":"00000000-0000-0000-0000-000000000002",'
                            b'"camera_id":"00000000-0000-0000-0000-000000000003",'
                            b'"occurred_at":"2026-08-19T00:00:00Z",'
                            b'"defect_class":"scratch","confidence":0.964}'
                        ),
                        b"updated_at": b"2026-08-19T00:00:00+00:00",
                        b"line_id": b"",
                    },
                )
            ]

    stored = RedisStreamInspectionAlertFeed(ByteRedisStream()).list(
        uuid4(), datetime(2026, 8, 18, tzinfo=UTC)
    )

    assert stored == []
    assert RedisStreamInspectionAlertFeed(ByteRedisStream()).list(
        UUID("00000000-0000-0000-0000-000000000002")
    )[0].alert.defect_class == "scratch"


def test_runtime_composes_redis_stream_for_notification_reconciliation_and_task_work() -> None:
    """Runtime must not silently fall back to an in-process feed or task queue."""
    class FakeRedisStream:
        def __init__(self) -> None:
            self.entries: list[tuple[str, dict[str, str]]] = []

        def xadd(self, stream: str, fields: dict[str, str]) -> str:
            self.entries.append((stream, fields))
            return f"{len(self.entries)}-0"

        def xlen(self, stream: str) -> int:
            return len([entry for entry in self.entries if entry[0] == stream])

        def xrange(self, stream: str) -> list[tuple[str, dict[str, str]]]:
            return [entry for entry in self.entries if entry[0] == stream]

    stream = FakeRedisStream()
    app = create_app(stream_client=stream)

    assert isinstance(app.state.task_service._queue, RedisStreamTaskQueue)
    assert any(entry[0] == "odp:inspection-alerts" for entry in stream.entries)
