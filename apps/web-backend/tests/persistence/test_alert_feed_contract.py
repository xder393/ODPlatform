"""Contract tests for the durable cursor-based inspection alert feed."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from odp_api.adapters.notifications.redis_durable_feed import RedisDurableInspectionAlertFeed
from odp_api.adapters.notifications.sqlite_feed import SqliteInspectionAlertFeed
from odp_api.adapters.persistence.models import Base
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
        def xread(self, stream, cursor, block_ms): self.xread_calls.append((stream, cursor, block_ms)); return self.response
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


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_feed_rejects_invalid_reconciliation_limit(feed, limit: int) -> None:
    """Accepting unbounded/invalid limits makes reconciliation unsafe."""
    with pytest.raises(ValueError):
        feed.list(ORG_ID, None, limit)
