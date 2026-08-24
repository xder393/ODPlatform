"""Contract tests for the durable cursor-based inspection alert feed."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

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


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_feed_rejects_invalid_reconciliation_limit(feed, limit: int) -> None:
    """Accepting unbounded/invalid limits makes reconciliation unsafe."""
    with pytest.raises(ValueError):
        feed.list(ORG_ID, None, limit)
