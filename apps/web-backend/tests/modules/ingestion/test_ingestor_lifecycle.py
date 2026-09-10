import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from odp_api.ports.inspection_sessions import IngestionClaim
from odp_api.processes.frame_ingestor import FrameIngestor


def claimed(name):
    session = SimpleNamespace(session_id=name)
    return SimpleNamespace(
        session=session,
        session_id=name,
        claim=IngestionClaim(uuid4(), uuid4(), uuid4(), uuid4(), 1),
    )


def test_claimed_cameras_start_concurrently():
    async def scenario():
        both_started = asyncio.Event()
        seen = []

        async def run_session(session, max_frames):
            seen.append(session.session_id)
            if len(seen) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 0.5)
            return session.session_id

        sessions = SimpleNamespace(
            claim_available=lambda *args: [
                claimed("first"), claimed("second")
            ],
            claim_stop_requests=lambda *args: [],
            mark_failed=lambda *args: True,
        )
        process = FrameIngestor(
            sessions, None, None, None, None, process_id="test", instance_id=uuid4()
        )
        process._run_session = run_session
        result = await process.run_once()
        assert result.failed == 0
        assert set(result.reports) == {"first", "second"}

    asyncio.run(scenario())


def test_stop_one_camera_releases_slot_without_stopping_other_camera():
    async def scenario():
        request_stop = threading.Event()
        both_started = asyncio.Event()
        replacement_started = asyncio.Event()
        stop = asyncio.Event()
        opened, closed = set(), set()
        claims = 0
        delivered_stop = False

        def claim(owner, instance_id, limit):
            nonlocal claims
            claims += 1
            if claims == 1:
                assert limit == 2
                return [claimed(name) for name in ("one", "two")]
            assert limit == 1
            assert "one" in closed
            return [claimed("three")]

        def stops(*args):
            nonlocal delivered_stop
            if request_stop.is_set() and not delivered_stop:
                delivered_stop = True
                return [SimpleNamespace(session_id="one")]
            return []

        async def stream(session, max_frames):
            name = session.session_id
            opened.add(name)
            if {"one", "two"} <= opened:
                both_started.set()
            if name == "three":
                replacement_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.add(name)

        sessions = SimpleNamespace(claim_available=claim, claim_stop_requests=stops)
        process = FrameIngestor(
            sessions, None, None, None, None, process_id="test",
            instance_id=uuid4(),
            max_concurrent_sessions=2, poll_interval_seconds=0.01,
        )
        process._run_session = stream
        running = asyncio.create_task(process.run(stop))
        try:
            await asyncio.wait_for(both_started.wait(), 1)
            request_stop.set()
            await asyncio.wait_for(replacement_started.wait(), 1)
            assert closed == {"one"}
            stop.set()
            await asyncio.wait_for(running, 1)
            assert closed == {"one", "two", "three"}
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_process_runs_and_drains_active_iteration_on_stop():
    async def scenario():
        started = asyncio.Event()
        drained = asyncio.Event()
        stop = asyncio.Event()

        async def step(session, max_frames):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        sessions = SimpleNamespace(
            claim_available=Mock(side_effect=[[claimed("one")], []]),
            claim_stop_requests=Mock(return_value=[]),
        )
        process = FrameIngestor(
            sessions, None, None, None, None, process_id="test", instance_id=uuid4()
        )
        process._run_session = step
        running = asyncio.create_task(process.run(stop))
        try:
            await asyncio.wait_for(started.wait(), 1)
            stop.set()
            await asyncio.wait_for(running, 1)
            assert drained.is_set()
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_already_stopped_process_does_not_claim_sessions():
    async def scenario():
        stop = asyncio.Event()
        stop.set()
        sessions = SimpleNamespace(claim_available=Mock())
        process = FrameIngestor(
            sessions, None, None, None, None, process_id="test", instance_id=uuid4()
        )
        await process.run(stop)
        sessions.claim_available.assert_not_called()

    asyncio.run(scenario())


def test_running_stream_does_not_block_later_camera_and_capacity_is_bounded():
    async def scenario():
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        stop = asyncio.Event()
        seen_limits = []
        drained = []

        def claim(owner, instance_id, limit):
            seen_limits.append(limit)
            if len(seen_limits) == 1:
                return [claimed("first")]
            if len(seen_limits) == 2:
                assert first_started.is_set()
                return [claimed("second")]
            raise AssertionError("claimed more sessions while capacity was full")

        async def run_session(session, max_frames):
            (first_started if session.session_id == "first" else second_started).set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.append(session.session_id)

        sessions = SimpleNamespace(
            claim_available=claim,
            claim_stop_requests=Mock(return_value=[]),
        )
        process = FrameIngestor(
            sessions, None, None, None, None, process_id="test",
            instance_id=uuid4(),
            max_concurrent_sessions=2, poll_interval_seconds=0.01,
        )
        process._run_session = run_session
        running = asyncio.create_task(process.run(stop))
        try:
            await asyncio.wait_for(second_started.wait(), 1)
            await asyncio.sleep(0.04)
            assert seen_limits == [2, 1]
            assert sessions.claim_stop_requests.call_count >= 3
            stop.set()
            await asyncio.wait_for(running, 1)
            assert set(drained) == {"first", "second"}
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
