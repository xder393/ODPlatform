import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.adapters.notifications.redis_stream import RedisStreamInspectionAlertFeed
from odp_api.adapters.redis_stream import RedisSocketStreamClient
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


def test_runtime_fixture_alert_is_idempotent_across_app_restarts() -> None:
    """A new app composition must not emit another copy of the stable demo alert."""
    class FakeRedisStream:
        def __init__(self) -> None:
            self.entries: list[tuple[str, dict[str, str]]] = []
            self.keys: set[str] = set()

        def xadd(self, stream: str, fields: dict[str, str]) -> str:
            self.entries.append((stream, fields))
            return f"{len(self.entries)}-0"

        def xlen(self, stream: str) -> int:
            return len([entry for entry in self.entries if entry[0] == stream])

        def xrange(self, stream: str) -> list[tuple[str, dict[str, str]]]:
            return [entry for entry in self.entries if entry[0] == stream]

        def setnx(self, key: str, value: str) -> bool:
            if key in self.keys:
                return False
            self.keys.add(key)
            return True

    stream = FakeRedisStream()

    create_app(stream_client=stream)
    create_app(stream_client=stream)

    assert len([entry for entry in stream.entries if entry[0] == "odp:inspection-alerts"]) == 1


def test_atomic_alert_claim_recovers_after_a_pre_append_failure_without_duplication() -> None:
    """Persisting a SET NX claim before XADD would lose this alert on retry."""
    class AtomicFakeRedisStream:
        def __init__(self) -> None:
            self.claims: set[str] = set()
            self.entries: list[tuple[str, dict[str, str]]] = []
            self.fail_after_claim = True

        def setnx(self, key: str, value: str) -> bool:
            if key in self.claims:
                return False
            self.claims.add(key)
            return True

        def xadd(self, stream: str, fields: dict[str, str]) -> str:
            if self.fail_after_claim:
                self.fail_after_claim = False
                raise OSError("failure after SET NX and before XADD")
            self.entries.append((stream, fields))
            return "1-0"

        def eval(self, script: str, keys: list[str], args: list[str]) -> str | None:
            claim_key, stream = keys
            if claim_key in self.claims:
                return None
            self.claims.add(claim_key)
            if self.fail_after_claim:
                self.fail_after_claim = False
                self.claims.remove(claim_key)
                raise OSError("atomic operation rolled back before XADD")
            self.entries.append((stream, dict(zip(args[::2], args[1::2], strict=True))))
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
    redis = AtomicFakeRedisStream()
    feed = RedisStreamInspectionAlertFeed(redis)

    with pytest.raises(OSError, match="rolled back"):
        feed.publish(alert, line_id=None)
    feed.publish(alert, line_id=None)
    feed.publish(alert, line_id=None)

    assert len(redis.entries) == 1


def test_rediss_stream_client_selects_tls_for_the_parsed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treating rediss as plain Redis would expose credentials and stream contents."""
    import io

    class FakeStream:
        def __init__(self) -> None:
            self.responses = io.BytesIO(b"+OK\r\n+OK\r\n:0\r\n")

        def write(self, _: bytes) -> int:
            return 0

        def flush(self) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self.responses.read(size)

        def readline(self) -> bytes:
            return self.responses.readline()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def makefile(self, _: str) -> FakeStream:
            return FakeStream()

    class FakeContext:
        def __init__(self) -> None:
            self.server_hostname: str | None = None

        def wrap_socket(self, connection: FakeConnection, *, server_hostname: str) -> FakeConnection:
            self.server_hostname = server_hostname
            return connection

    context = FakeContext()
    from odp_api.adapters import redis_stream

    monkeypatch.setattr(redis_stream.socket, "create_connection", lambda address, timeout: FakeConnection())
    monkeypatch.setattr(redis_stream.ssl, "create_default_context", lambda: context)

    assert RedisSocketStreamClient("rediss://:secret@example.test:6380/4").xlen("odp:tasks") == 0
    assert context.server_hostname == "example.test"
