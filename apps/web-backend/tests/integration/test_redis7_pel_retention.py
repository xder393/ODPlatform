"""Real Redis 7 proof that exact retention preserves Pending payloads."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]
REDIS_URL = os.getenv("ODP_REDIS_TEST_URL")


@pytest.mark.skipif(
    not REDIS_URL,
    reason="requires the dedicated ODP_REDIS_TEST_URL CI Redis 7 service",
)
async def test_pending_payload_survives_until_ack_and_forward_group_progress():
    """A real XRANGE payload, not merely XLEN, guards the PEL safety promise."""
    from redis.asyncio import Redis

    from odp_api.modules.tasks.retention import (
        MinimumRetentionPolicy,
        RedisRetentionAdapter,
        StreamRetentionController,
        StreamRetentionPolicy,
    )

    stream = f"odp:test:pel-retention:{uuid4()}"
    group = "retention-test-workers"
    redis = Redis.from_url(REDIS_URL, decode_responses=False)
    try:
        server = await redis.info("server")
        assert str(server["redis_version"]).split(".", maxsplit=1)[0] == "7"
        await redis.xadd(stream, {"payload": "must-survive"}, id="1000-0")
        await redis.xgroup_create(stream, group, id="0-0")
        delivered = await redis.xreadgroup(group, "worker-a", {stream: ">"}, count=1)
        assert delivered[0][1][0] == (b"1000-0", {b"payload": b"must-survive"})
        await redis.xadd(stream, {"payload": "forward-progress"}, id="2000-0")
        await redis.xgroup_create(stream, "retention-observer", id="$", mkstream=False)

        controller = StreamRetentionController(
            RedisRetentionAdapter(redis),
            StreamRetentionPolicy(
                stream, MinimumRetentionPolicy(timedelta(milliseconds=500))
            ),
            clock=lambda: datetime.fromtimestamp(3, tz=UTC),
        )
        pending_summary = await controller.run_once()
        assert pending_summary.safe_min_id == "1000-0"
        assert pending_summary.groups_considered == 2
        assert await redis.xrange(stream, min="1000-0", max="1000-0") == [
            (b"1000-0", {b"payload": b"must-survive"})
        ]

        assert await redis.xack(stream, group, "1000-0") == 1
        progressed = await redis.xreadgroup(group, "worker-a", {stream: ">"}, count=1)
        assert progressed[0][1][0][0] == b"2000-0"
        acknowledged_summary = await controller.run_once()
        assert acknowledged_summary.safe_min_id == "2000-0"
        assert await redis.xrange(stream, min="1000-0", max="1000-0") == []
        assert await redis.xrange(stream, min="2000-0", max="2000-0") == [
            (b"2000-0", {b"payload": b"forward-progress"})
        ]
    finally:
        await redis.delete(stream)
        await redis.aclose()


@pytest.mark.skipif(
    not REDIS_URL,
    reason="requires the dedicated ODP_REDIS_TEST_URL CI Redis 7 service",
)
async def test_rewound_group_with_later_pending_entry_disables_trimming():
    """XGROUP SETID rewind must preserve replayable history below the PEL minimum."""
    from redis.asyncio import Redis

    from odp_api.modules.tasks.retention import (
        MinimumRetentionPolicy,
        RedisRetentionAdapter,
        StreamRetentionController,
        StreamRetentionPolicy,
    )

    stream = f"odp:test:pel-rewind:{uuid4()}"
    group = "rewound-retention-workers"
    redis = Redis.from_url(REDIS_URL, decode_responses=False)
    try:
        await redis.xadd(stream, {"payload": "must-remain-replayable"}, id="1000-0")
        await redis.xadd(stream, {"payload": "still-pending"}, id="2000-0")
        await redis.xgroup_create(stream, group, id="1000-0")
        delivered = await redis.xreadgroup(group, "worker-a", {stream: ">"}, count=1)
        assert delivered[0][1][0][0] == b"2000-0"
        await redis.xgroup_setid(stream, group, "1000-0")

        controller = StreamRetentionController(
            RedisRetentionAdapter(redis),
            StreamRetentionPolicy(
                stream, MinimumRetentionPolicy(timedelta(milliseconds=500))
            ),
            clock=lambda: datetime.fromtimestamp(3, tz=UTC),
        )
        summary = await controller.run_once()

        assert summary.safe_min_id is None
        assert summary.trimmed_entries == 0
        assert await redis.xrange(stream, min="1000-0", max="1000-0") == [
            (b"1000-0", {b"payload": b"must-remain-replayable"})
        ]
    finally:
        await redis.delete(stream)
        await redis.aclose()
