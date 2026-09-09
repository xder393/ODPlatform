"""Real Redis TTL/renewal checks; database pressure is held constant."""

import asyncio
import os
from contextlib import nullcontext
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from odp_api.adapters.redis_stream import RedisSocketStreamClient
from odp_api.processes.runtime import DatabaseIngestionHealth
from odp_api.processes.worker_liveness import (
    HEARTBEAT_TTL_SECONDS,
    RedisWorkerHeartbeat,
    liveness_key,
)

REDIS_URL = os.getenv("ODP_REDIS_TEST_URL")


@pytest.mark.skipif(not REDIS_URL, reason="ODP_REDIS_TEST_URL is required")
def test_real_redis_presence_expires_and_recovers_without_cross_model_leakage():
    async def scenario():
        client = Redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=2)
        digest = sha256(uuid4().bytes).hexdigest()
        other_digest = sha256(uuid4().bytes).hexdigest()
        keys = (liveness_key(digest), liveness_key(other_digest))
        sessions = lambda: nullcontext(SimpleNamespace(scalar=lambda query: None))
        health = DatabaseIngestionHealth(sessions, RedisSocketStreamClient(REDIS_URL), digest)
        first = RedisWorkerHeartbeat(client, digest)
        second = RedisWorkerHeartbeat(client, digest)
        try:
            absent = await health.snapshot(None, None)
            assert absent.redis_available and not absent.worker_healthy
            await RedisWorkerHeartbeat(client, other_digest)()
            assert not (await health.snapshot(None, None)).worker_healthy
            await first()
            assert (await health.snapshot(None, None)).worker_healthy
            assert 0 < await client.pttl(keys[0]) <= HEARTBEAT_TTL_SECONDS * 1000
            # Real expiry at the production TTL, without changing server time.
            deadline = asyncio.get_running_loop().time() + HEARTBEAT_TTL_SECONDS + 3
            while await client.exists(keys[0]):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.1)
            expired = await health.snapshot(None, None)
            assert expired.redis_available and not expired.worker_healthy
            await second()
            assert (await health.snapshot(None, None)).worker_healthy
            assert await client.pttl(keys[0]) > (HEARTBEAT_TTL_SECONDS - 1) * 1000
        finally:
            try:
                await client.delete(*keys)
            finally:
                await client.aclose()

    asyncio.run(scenario())


def test_refused_redis_connection_is_not_reported_as_worker_offline_only():
    import socket

    async def scenario():
        # Reserve a port without listening: no unrelated Redis is stopped.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
            sessions = lambda: nullcontext(SimpleNamespace(scalar=lambda query: None))
            health = DatabaseIngestionHealth(
                sessions, RedisSocketStreamClient(f"redis://127.0.0.1:{port}"), "a" * 64
            )
            state = await health.snapshot(None, None)
            assert not state.redis_available and not state.worker_healthy

    asyncio.run(scenario())
