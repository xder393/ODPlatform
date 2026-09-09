import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from odp_api.processes.inference_worker import InferenceWorker


def test_heartbeat_continues_during_busy_worker_and_stops_on_shutdown():
    async def scenario():
        stop = asyncio.Event()
        second_pulse = asyncio.Event()
        pulses = 0

        async def pulse():
            nonlocal pulses
            pulses += 1
            if pulses >= 2:
                second_pulse.set()

        worker = InferenceWorker(
            SimpleNamespace(start=AsyncMock()), SimpleNamespace(worker_id="one"),
            None, None, None, heartbeat=pulse, heartbeat_interval_seconds=0.01,
        )

        async def busy():
            await asyncio.Event().wait()

        worker.run_once = busy
        task = asyncio.create_task(worker.run(stop))
        try:
            await asyncio.wait_for(second_pulse.wait(), 1)
            stop.set()
            await asyncio.wait_for(task, 1)
            count = pulses
            await asyncio.sleep(0.03)
            assert pulses == count
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_admission_presence_expires_recovers_and_is_model_scoped():
    from odp_api.processes.runtime import DatabaseIngestionHealth
    from odp_api.processes.worker_liveness import RedisWorkerHeartbeat

    class Redis:
        now = 0
        available = True

        def __init__(self):
            self.values = {}

        async def set(self, key, value, ex):
            self.values[key] = (value, self.now + ex)
            return True

        def get(self, key):
            if not self.available:
                raise ConnectionError("offline")
            value, expires = self.values.get(key, (None, 0))
            return value if expires > self.now else None

    async def scenario():
        redis = Redis()
        sessions = lambda: nullcontext(SimpleNamespace(scalar=lambda query: None))
        health = DatabaseIngestionHealth(sessions, redis, "a" * 64)
        worker1 = RedisWorkerHeartbeat(redis, "a" * 64)
        worker2 = RedisWorkerHeartbeat(redis, "a" * 64)
        other_model = RedisWorkerHeartbeat(redis, "b" * 64)
        assert not (await health.snapshot(None, None)).worker_healthy
        await other_model()
        assert not (await health.snapshot(None, None)).worker_healthy
        await worker1()
        assert (await health.snapshot(None, None)).worker_healthy
        redis.now = 10
        await worker2()
        redis.now = 16
        assert (await health.snapshot(None, None)).worker_healthy
        redis.now = 25
        expired = await health.snapshot(None, None)
        assert expired.redis_available and not expired.worker_healthy
        await worker1()
        assert (await health.snapshot(None, None)).worker_healthy
        redis.available = False
        offline = await health.snapshot(None, None)
        assert not offline.redis_available and not offline.worker_healthy

    asyncio.run(scenario())


def test_failed_initial_heartbeat_prevents_consumption():
    async def scenario():
        worker = InferenceWorker(
            SimpleNamespace(start=AsyncMock()), SimpleNamespace(worker_id="one"),
            None, None, None, heartbeat=AsyncMock(side_effect=ConnectionError("redis")),
        )
        worker.run_once = AsyncMock()
        with pytest.raises(ConnectionError):
            await worker.run(asyncio.Event())
        worker.run_once.assert_not_awaited()

    asyncio.run(scenario())
