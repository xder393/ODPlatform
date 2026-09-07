"""Realtime Gateway consumes Redis wake-ups but reads alert facts durably."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from odp_schemas.events import EventEnvelope, InspectionAlert

from odp_api.adapters.notifications.redis_gateway_feed import (
    RedisGatewayInspectionAlertFeed,
)
from odp_api.adapters.notifications.sqlite_feed import SqliteInspectionAlertFeed
from odp_api.adapters.persistence.models import Base
from odp_api.db import create_engine_and_session

ORG_ID = UUID("10000000-0000-4000-8000-000000000001")
LINE_ID = UUID("20000000-0000-4000-8000-000000000001")


class FakeRedis:
    def __init__(self, envelope: str) -> None:
        self.envelope = envelope
        self.acks: list[tuple[str, str, str]] = []
        self.groups: list[tuple[str, str, str]] = []
        self.destroyed: list[tuple[str, str]] = []
        self.reads = 0

    async def xgroup_create(self, name, groupname, id, mkstream):
        self.groups.append((name, groupname, id))

    async def xreadgroup(self, *, groupname, consumername, streams, count, block):
        self.reads += 1
        if self.reads == 1:
            return [(next(iter(streams)), [("1-0", {"envelope": self.envelope})])]
        await asyncio.sleep(0.05)
        return []

    async def xack(self, name, groupname, *ids):
        self.acks.extend((name, groupname, message_id) for message_id in ids)

    async def xgroup_destroy(self, name, groupname):
        self.destroyed.append((name, groupname))
        return 1

    async def aclose(self):
        return None


@pytest.fixture
def facts(tmp_path: Path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'gateway.db'}")
    Base.metadata.create_all(engine)
    try:
        yield SqliteInspectionAlertFeed(sessions)
    finally:
        engine.dispose()


def _alert() -> InspectionAlert:
    return InspectionAlert(
        event_id=uuid4(),
        organization_id=ORG_ID,
        camera_id=uuid4(),
        occurred_at=datetime(2026, 8, 25, tzinfo=UTC),
        defect_class="scratch",
        confidence=0.964,
    )


def _envelope(alert: InspectionAlert, cursor: str) -> str:
    return EventEnvelope(
        event_id=alert.event_id,
        event_type="inspection.alert.created.v1",
        schema_version=1,
        occurred_at=alert.occurred_at,
        correlation_id=alert.event_id,
        organization_id=alert.organization_id,
        aggregate_id=alert.event_id,
        payload={
            "alert_id": str(alert.event_id),
            "organization_id": str(alert.organization_id),
            "case_id": str(uuid4()),
            "event_id": str(alert.event_id),
            "camera_id": str(alert.camera_id),
            "line_id": str(LINE_ID),
            "defect_type": alert.defect_class,
            "severity": "MEDIUM",
            "confidence": alert.confidence,
            "occurred_at": alert.occurred_at.isoformat(),
            "business_cursor": cursor,
        },
    ).canonical_json()


@pytest.mark.anyio
async def test_gateway_requeries_facts_and_acks_only_after_delivery(facts) -> None:
    alert = _alert()
    cursor = facts.publish(alert, LINE_ID)
    redis = FakeRedis(_envelope(alert, cursor))
    gateway = RedisGatewayInspectionAlertFeed(
        facts,
        redis,
        instance_id="gateway-a",
        async_client_factory=lambda: redis,
    )

    subscription = gateway.subscribe(None)
    received = await asyncio.wait_for(anext(subscription), timeout=1)
    assert received.alert.event_id == alert.event_id
    assert redis.acks == []

    waiting = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)
    assert redis.acks == [("odp:inspection:alerts", "odp-alert-gateway:gateway-a", "1-0")]
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await subscription.aclose()


@pytest.mark.anyio
async def test_gateway_group_is_destroyed_on_process_close(facts) -> None:
    redis = FakeRedis(json.dumps({}))
    gateway = RedisGatewayInspectionAlertFeed(
        facts, redis, instance_id="gateway-b", async_client_factory=lambda: redis
    )
    await gateway.close()
    assert redis.destroyed == [("odp:inspection:alerts", "odp-alert-gateway:gateway-b")]


def test_gateway_is_read_only_and_has_no_publish_method(facts) -> None:
    gateway = RedisGatewayInspectionAlertFeed(
        facts,
        FakeRedis(json.dumps({})),
        instance_id="gateway-c",
        async_client_factory=lambda: None,
    )
    assert not hasattr(gateway, "publish")
