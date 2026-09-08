"""A nonempty transport backlog must not pay an idle delay per batch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from odp_api.processes.inference_worker import InferenceWorker


def test_worker_drains_backlog_without_idle_delay_between_batches():
    async def scenario():
        stop = asyncio.Event()
        drained = asyncio.Event()
        processed = []
        consumer = SimpleNamespace(start=AsyncMock(), claim_stale=AsyncMock(return_value=[]),
                                   read_new=AsyncMock(side_effect=[[1], [2], [3]]))
        worker = InferenceWorker(consumer, SimpleNamespace(worker_id="test"), None, None, None)

        async def process(message):
            processed.append(message)
            if len(processed) == 3:
                drained.set()
                stop.set()

        worker.process = process
        runner = asyncio.create_task(worker.run(stop))
        try:
            await asyncio.wait_for(drained.wait(), 0.5)
            await asyncio.wait_for(runner, 0.5)
            assert processed == [1, 2, 3]
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_empty_queue_backs_off_and_stop_remains_responsive():
    async def scenario():
        stop, first_read = asyncio.Event(), asyncio.Event()
        reads = 0

        async def read():
            nonlocal reads
            reads += 1
            first_read.set()
            return []

        consumer = SimpleNamespace(start=AsyncMock(), claim_stale=AsyncMock(return_value=[]),
                                   read_new=read)
        worker = InferenceWorker(consumer, SimpleNamespace(worker_id="test"), None, None, None)
        runner = asyncio.create_task(worker.run(stop))
        try:
            await asyncio.wait_for(first_read.wait(), 0.5)
            await asyncio.sleep(0.05)
            assert reads == 1
            stop.set()
            await asyncio.wait_for(runner, 0.5)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())
