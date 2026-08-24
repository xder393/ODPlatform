"""Contract tests for the durable cursor-based inspection alert feed."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import UTC, datetime
from pathlib import Path
import json
import sys
from types import ModuleType
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from odp_api.adapters.notifications.redis_durable_feed import RedisDurableInspectionAlertFeed
from odp_api.adapters.notifications.sqlite_feed import SqliteInspectionAlertFeed
from odp_api.adapters.redis_stream import RedisSocketStreamClient
from odp_api.adapters.persistence.models import Base, InspectionAlertFeedRow
from odp_api.db import create_engine_and_session
from odp_schemas.events import InspectionAlert


ORG_ID = UUID("10000000-0000-4000-8000-000000000001")
LINE_ID = UUID("20000000-0000-4000-8000-000000000001")


@pytest.fixture
def feed(tmp_path: Path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'feed.db'}")
    Base.metadata.create_all(engine)
    try:
        yield SqliteInspectionAlertFeed(sessions)
    finally:
        engine.dispose()


def _alert(event_id=None, organization_id=ORG_ID) -> InspectionAlert:
    return InspectionAlert(
        event_id=event_id or uuid4(), organization_id=organization_id, camera_id=uuid4(),
        occurred_at=datetime(2026, 8, 24, tzinfo=UTC), defect_class="scratch", confidence=0.964,
    )


@pytest.mark.anyio
async def test_subscriber_receives_event_published_after_subscription(feed) -> None:
    """Removing the durable re-query after a wake-up loses a live event."""
    subscription = feed.subscribe(None)
    pending = anext(subscription)
    await asyncio.sleep(0)
    alert = _alert()
    cursor = feed.publish(alert, LINE_ID)
    received = await asyncio.wait_for(pending, timeout=1)

    assert received.cursor == cursor
    assert received.alert.event_id == alert.event_id
    await subscription.aclose()


def test_reconciliation_is_incremental_idempotent_and_tenant_scoped(feed) -> None:
    """Dropping cursor/event uniqueness could replay duplicates or leak another tenant."""
    first_alert = _alert()
    first = feed.publish(first_alert, LINE_ID)
    assert feed.publish(first_alert, LINE_ID) == first
    second = feed.publish(_alert(), LINE_ID)
    feed.publish(_alert(organization_id=uuid4()), LINE_ID)

    assert [item.cursor for item in feed.list(ORG_ID, first, 100)] == [second]
    assert [item.cursor for item in feed.list(ORG_ID, None, 100)] == [first, second]


def test_same_event_id_is_idempotent_only_inside_its_tenant(feed) -> None:
    """A global event-id uniqueness constraint rejects a valid foreign tenant fact."""
    event_id = uuid4()
    first = feed.publish(_alert(event_id), LINE_ID)
    foreign_org = uuid4()
    second = feed.publish(_alert(event_id, foreign_org), LINE_ID)
    assert first != second
    assert [item.cursor for item in feed.list(foreign_org, None, 100)] == [second]


def test_concurrent_duplicate_publish_returns_the_committed_winner(feed) -> None:
    """A racing uniqueness conflict must roll back then return the stored cursor."""
    alert = _alert()
    with ThreadPoolExecutor(max_workers=2) as executor:
        cursors = list(executor.map(lambda _: feed.publish(alert, LINE_ID), range(2)))
    assert cursors[0] == cursors[1]
    assert len(feed.list(ORG_ID, None, 100)) == 1


def test_barrier_forces_duplicate_unique_race_and_returns_winner(feed, monkeypatch) -> None:
    """Both writers cross the absent check before one loses the DB unique race."""
    original = feed._existing; barrier = Barrier(2)
    def raced(session, org, event):
        result = original(session, org, event)
        if result is None: barrier.wait(timeout=1)
        return result
    monkeypatch.setattr(feed, "_existing", raced)
    alert = _alert()
    with ThreadPoolExecutor(max_workers=2) as executor:
        cursors = list(executor.map(lambda _: feed.publish(alert, LINE_ID), range(2)))
    assert cursors[0] == cursors[1]
    assert len(feed.list(ORG_ID, None, 100)) == 1


def test_async_redis_client_uses_separate_connect_and_read_timeouts(monkeypatch) -> None:
    """A socket timeout below XREAD BLOCK would abort normal 15-second reads."""
    captured = {}
    async_module = ModuleType("redis.asyncio")
    async_module.Redis = type("Redis", (), {"from_url": staticmethod(lambda url, **kwargs: captured.update(url=url, **kwargs) or object())})
    redis_module = ModuleType("redis"); redis_module.asyncio = async_module
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    monkeypatch.setitem(sys.modules, "redis.asyncio", async_module)
    RedisSocketStreamClient("redis://:secret@redis.example:6380/2").async_client()
    assert captured["url"] == "redis://:secret@redis.example:6380/2"
    assert captured["socket_connect_timeout"] == 1
    assert captured["socket_timeout"] == 20
    assert captured["decode_responses"] is True


def test_alembic_upgrades_original_0004_alert_data_to_tenant_scoped_uniqueness(tmp_path: Path) -> None:
    """Existing global-event unique data must survive the 0004→head migration."""
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0004_durable_inspection_alert_feed")
    engine, sessions = create_engine_and_session(database_url)
    alert = _alert()
    with sessions.begin() as session:
        session.add(InspectionAlertFeedRow(event_id=alert.event_id, organization_id=ORG_ID, line_id=LINE_ID, payload=alert.model_dump(mode="json"), created_at=alert.occurred_at))
    command.upgrade(config, "head")
    feed = SqliteInspectionAlertFeed(sessions)
    assert feed.publish(alert, LINE_ID) == "1"
    assert feed.publish(_alert(alert.event_id, uuid4()), LINE_ID) == "2"
    engine.dispose()


def test_authorized_line_filter_applies_before_limit(feed) -> None:
    """Post-limit filtering lets an inaccessible first page starve authorized events."""
    feed.publish(_alert(), uuid4())
    visible = feed.publish(_alert(), LINE_ID)
    assert [item.cursor for item in feed.list(ORG_ID, None, 1, frozenset({LINE_ID}))] == [visible]


@pytest.mark.anyio
async def test_redis_wakeup_requeries_durable_facts_and_uses_bounded_commands(feed) -> None:
    """Redis is only a blocking wake-up; its response must never be the alert fact."""
    class FakeRedis:
        def __init__(self): self.xadd_calls = []; self.xread_calls = []; self.response = [["s", [["9-0", []]]]]
        def xadd_bounded(self, stream, fields, maxlen): self.xadd_calls.append((stream, fields, maxlen)); return "9-0"
        def async_client(self): return self
        async def xrevrange(self, stream, count): return []
        async def xread(self, streams, count, block):
            self.xread_calls.append((streams, count, block))
            await asyncio.sleep(0.01)
            return None
        async def aclose(self): return None
    redis = FakeRedis()
    durable = RedisDurableInspectionAlertFeed(feed, redis)
    subscription = durable.subscribe(None)
    pending = anext(subscription)
    await asyncio.sleep(0)
    alert = _alert()
    cursor = durable.publish(alert, LINE_ID)
    received = await asyncio.wait_for(pending, 1)
    waiting = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)
    assert received.cursor == cursor
    assert redis.xadd_calls[0][2] == 10_000
    assert redis.xread_calls[0][2] == 15_000
    waiting.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await waiting
    await subscription.aclose()


@pytest.mark.anyio
async def test_redis_highwater_interleaving_publishes_before_first_xread(feed, monkeypatch) -> None:
    """An event after an empty fact query wakes XREAD from captured high-water."""
    class AsyncRedis:
        def __init__(self): self.xread_calls = []; self.closed = False
        async def xrevrange(self, stream, count): return [("8-0", {})]
        async def xread(self, streams, count, block):
            self.xread_calls.append((streams, count, block))
            feed.publish(alert, LINE_ID)
            return [("odp:inspection-alerts", [("9-0", {})])]
        async def aclose(self): self.closed = True
    class Redis:
        def __init__(self): self.async_instance = AsyncRedis()
        def xadd_bounded(self, stream, fields, maxlen): return "9-0"
        def async_client(self): return self.async_instance
    redis = Redis()
    durable = RedisDurableInspectionAlertFeed(feed, redis)
    alert = _alert()
    original = feed._list_after; calls = 0
    def interleave(cursor):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else original(cursor)
    monkeypatch.setattr(feed, "_list_after", interleave)
    subscription = durable.subscribe(None)
    received = await asyncio.wait_for(anext(subscription), 0.2)
    assert received.alert.event_id == alert.event_id
    assert redis.async_instance.xread_calls == [({"odp:inspection-alerts": "8-0"}, 100, 15_000)]
    await subscription.aclose()
    assert redis.async_instance.closed


@pytest.mark.anyio
async def test_redis_timeout_requeries_and_cancellation_closes_client(feed) -> None:
    """A timeout is a wake-up hint; cancellation must close the async connection."""
    class AsyncRedis:
        def __init__(self): self.calls = 0; self.closed = False; self.started = asyncio.Event(); self.cancelled = False
        async def xrevrange(self, stream, count): return []
        async def xread(self, streams, count, block):
            self.calls += 1
            self.started.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        async def aclose(self): self.closed = True
    class Redis:
        def __init__(self): self.async_instance = AsyncRedis()
        def xadd_bounded(self, stream, fields, maxlen): return "1-0"
        def async_client(self): return self.async_instance
    redis = Redis(); durable = RedisDurableInspectionAlertFeed(feed, redis)
    subscription = durable.subscribe(None)
    pending = asyncio.create_task(anext(subscription))
    await redis.async_instance.started.wait()
    pending.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError): await pending
    await subscription.aclose()
    assert redis.async_instance.calls > 0 and redis.async_instance.closed and redis.async_instance.cancelled


@pytest.mark.anyio
async def test_redis_xread_failure_propagates_and_closes_client(feed) -> None:
    """Transport failure must close the socket path rather than become polling."""
    class AsyncRedis:
        def __init__(self): self.closed = False
        async def xrevrange(self, stream, count): return []
        async def xread(self, streams, count, block): raise OSError("redis unavailable")
        async def aclose(self): self.closed = True
    class Redis:
        def __init__(self): self.async_instance = AsyncRedis()
        def xadd_bounded(self, stream, fields, maxlen): return "1-0"
        def async_client(self): return self.async_instance
    redis = Redis(); subscription = RedisDurableInspectionAlertFeed(feed, redis).subscribe(None)
    with pytest.raises(OSError, match="unavailable"):
        await anext(subscription)
    assert redis.async_instance.closed


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_feed_rejects_invalid_reconciliation_limit(feed, limit: int) -> None:
    """Accepting unbounded/invalid limits makes reconciliation unsafe."""
    with pytest.raises(ValueError):
        feed.list(ORG_ID, None, limit)
