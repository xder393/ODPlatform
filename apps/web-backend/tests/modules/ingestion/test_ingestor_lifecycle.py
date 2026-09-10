import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from odp_api.modules.ingestion.artifacts import ArtifactHealth
from odp_api.modules.ingestion.service import IngestionReport
from odp_api.ports.inspection_sessions import IngestionClaim
from odp_api.processes.frame_ingestor import FrameIngestor


def claimed(name):
    session = SimpleNamespace(
        session_id=name,
        organization_id=uuid4(),
        camera_id=uuid4(),
        source_type="RECORDED",
        sanitized_uri="/safe/fixture.mp4",
    )
    return SimpleNamespace(
        session=session,
        session_id=name,
        claim=IngestionClaim(uuid4(), uuid4(), uuid4(), uuid4(), 1),
    )


def test_claimed_cameras_start_concurrently():
    async def scenario():
        both_started = asyncio.Event()
        seen = []

        async def run_session(claimed_session, max_frames):
            session = claimed_session.session
            seen.append(session.session_id)
            if len(seen) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 0.5)
            return session.session_id

        sessions = SimpleNamespace(
            claim_available=lambda *args: [
                claimed("first"), claimed("second")
            ],
            stop_candidates=lambda *args: [],
            finalize_expired_stops=lambda *args: 0,
            release=lambda *args: True,
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
        claimed_sessions = {name: claimed(name) for name in ("one", "two", "three")}

        def claim(owner, instance_id, limit):
            nonlocal claims
            claims += 1
            if claims == 1:
                assert limit == 2
                return [claimed_sessions[name] for name in ("one", "two")]
            assert limit == 1
            assert "one" in closed
            return [claimed_sessions["three"]]

        def stops(instance_id, limit):
            nonlocal delivered_stop
            if request_stop.is_set() and not delivered_stop:
                delivered_stop = True
                return [claimed_sessions["one"].claim]
            return []

        async def stream(claimed_session, max_frames):
            session = claimed_session.session
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

        sessions = SimpleNamespace(
            claim_available=claim,
            stop_candidates=stops,
            finalize_expired_stops=lambda *args: 0,
            finish_stop=Mock(return_value=True),
            release=Mock(return_value=True),
        )
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

        async def step(claimed_session, max_frames):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        sessions = SimpleNamespace(
            claim_available=Mock(side_effect=[[claimed("one")], []]),
            stop_candidates=Mock(return_value=[]),
            finalize_expired_stops=Mock(return_value=0),
            release=Mock(return_value=True),
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

        async def run_session(claimed_session, max_frames):
            session = claimed_session.session
            (first_started if session.session_id == "first" else second_started).set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.append(session.session_id)

        sessions = SimpleNamespace(
            claim_available=claim,
            stop_candidates=Mock(return_value=[]),
            finalize_expired_stops=Mock(return_value=0),
            release=Mock(return_value=True),
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
            assert sessions.stop_candidates.call_count >= 3
            stop.set()
            await asyncio.wait_for(running, 1)
            assert set(drained) == {"first", "second"}
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("renew_result", [False, RuntimeError("database unavailable")])
def test_renew_loss_stops_ingestion_and_closes_source(renew_result):
    async def scenario():
        started = asyncio.Event()
        drained = asyncio.Event()
        closed = asyncio.Event()
        claim = claimed("camera")

        class Source:
            async def frames(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
                yield None

            async def close(self):
                closed.set()

        class Ingestion:
            async def run(self, source, **_kwargs):
                async for _frame in source.frames():
                    pass

        def renew(_claim):
            if isinstance(renew_result, BaseException):
                raise renew_result
            return renew_result

        sessions = SimpleNamespace(
            renew=renew,
            fail_claim=Mock(return_value=False),
        )
        source = Source()
        process = FrameIngestor(
            sessions,
            Ingestion(),
            None,
            lambda received: source if received is claim else pytest.fail("claim was not passed to source factory"),
            lambda _session: None,
            process_id="test",
            instance_id=uuid4(),
            heartbeat_interval_seconds=0.001,
        )

        task = asyncio.create_task(process._run_claimed(claim, None))
        await asyncio.wait_for(started.wait(), 1)
        result = await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True), 1
        )
        assert isinstance(result[0], BaseException)
        assert drained.is_set()
        assert closed.is_set()

    asyncio.run(scenario())


def test_saga_setup_failure_still_closes_created_source():
    async def scenario():
        closed = asyncio.Event()
        claim = claimed("setup-failure")

        class Source:
            async def close(self):
                closed.set()

        def source_factory(received):
            assert received is claim
            return Source()

        def saga_factory(_session):
            raise RuntimeError("saga setup failed")

        class Ingestion:
            async def run(self, _source, **_kwargs):
                return IngestionReport()

        process = FrameIngestor(
            SimpleNamespace(),
            Ingestion(),
            None,
            source_factory,
            saga_factory,
            process_id="test",
            instance_id=uuid4(),
        )

        result = await asyncio.gather(
            asyncio.create_task(process._run_claimed(claim, None)),
            return_exceptions=True,
        )
        assert isinstance(result[0], RuntimeError)
        assert closed.is_set()

    asyncio.run(scenario())


def test_heartbeat_failure_wins_when_ingestion_finishes_in_same_wait():
    async def scenario():
        rendezvous = asyncio.Event()
        arrived = 0
        claim = claimed("simultaneous-failure")

        class Source:
            async def close(self):
                return None

        async def meet():
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                rendezvous.set()
            await rendezvous.wait()

        class Ingestion:
            async def run(self, _source, **_kwargs):
                await meet()
                return IngestionReport()

        async def heartbeat(_claimed):
            await meet()
            raise RuntimeError("renew failed")

        process = FrameIngestor(
            SimpleNamespace(),
            Ingestion(),
            ArtifactHealth(True, True),
            lambda _claimed: Source(),
            lambda _session: None,
            process_id="test",
            instance_id=uuid4(),
        )
        process._heartbeat_until_stop = heartbeat

        with pytest.raises(RuntimeError, match="renew failed"):
            await process._run_session(claim, None)

    asyncio.run(scenario())
