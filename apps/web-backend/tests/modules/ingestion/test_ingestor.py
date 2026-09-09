"""TDD contracts for sampled realtime frame ingestion."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from odp_api.modules.ingestion.artifacts import ArtifactHealth
from odp_api.modules.ingestion.service import IngestionService
from odp_api.modules.tasks.models import TaskRecord, TaskStatus
from odp_api.ports.frame_sources import DecodedFrame
from odp_api.ports.inspection_sessions import InspectionSession
from odp_api.ports.tasks import AdmissionRejected

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


@dataclass
class FakeClock:
    current: datetime = NOW

    def now(self):
        return self.current

    def advance(self, *, seconds):
        self.current += timedelta(seconds=seconds)


class FakeCapture:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        try:
            return True, next(self.frames)
        except StopIteration:
            return False, None

    def release(self):
        self.released = True


def test_recorded_video_uses_current_loop_wall_clock():
    from odp_api.adapters.frame_sources.opencv import RecordedVideoSource

    clock = FakeClock()
    captures = []

    def capture_factory(_path):
        capture = FakeCapture([b"frame"])
        captures.append(capture)
        return capture

    source = RecordedVideoSource(
        "fixture.mp4",
        camera_id=uuid4(),
        session_id=uuid4(),
        clock=clock.now,
        capture_factory=capture_factory,
    )
    first = None

    async def read_first():
        return await anext(source.frames())

    import asyncio

    first = asyncio.run(read_first())
    clock.advance(seconds=5)
    replayed = asyncio.run(source.restart_and_read_first())
    assert replayed.captured_at == clock.now()
    assert replayed.captured_at != first.captured_at
    assert captures[0].released


def test_ingestor_samples_before_encoding_and_rejects_without_upload():
    camera_id, session_id, organization_id = uuid4(), uuid4(), uuid4()
    frames = [
        DecodedFrame(camera_id, session_id, index + 1, NOW + timedelta(seconds=index / 10), b"raw")
        for index in range(10)
    ]

    class Source:
        async def frames(self):
            for item in frames:
                yield item

        async def close(self):
            return None

    class Health:
        def snapshot(self, _organization_id, _camera_id):
            return ArtifactHealth(False, True, 0, 0)

    class Encoder:
        calls = 0

        def encode(self, _frame):
            self.calls += 1
            return b"jpeg"

    class Saga:
        calls = 0

        def ingest(self, *_args):
            self.calls += 1
            return AdmissionRejected("unexpected")

    encoder, saga = Encoder(), Saga()
    import asyncio

    report = asyncio.run(
        IngestionService(encoder, clock=lambda: NOW).run(
            Source(),
            organization_id=organization_id,
            health=Health(),
            saga=saga,
            max_frames=10,
        )
    )

    assert (report.decoded, report.sampled, report.encoded, report.rejected) == (10, 2, 0, 2)
    assert encoder.calls == saga.calls == 0
    assert report.rejection_reasons == {"WORKER_UNHEALTHY": 2}


def test_ingestor_encodes_selected_frames_and_calls_saga_after_preflight():
    camera_id, session_id, organization_id = uuid4(), uuid4(), uuid4()
    frames = [
        DecodedFrame(camera_id, session_id, index + 1, NOW + timedelta(seconds=index / 10), b"raw")
        for index in range(10)
    ]

    class Source:
        async def frames(self):
            for item in frames:
                yield item

        async def close(self):
            return None

    class Health:
        def snapshot(self, _organization_id, _camera_id):
            return ArtifactHealth(True, True, 0, 0)

    class Encoder:
        def __init__(self):
            self.calls = []

        def encode(self, frame):
            self.calls.append(frame)
            return b"jpeg"

    class Saga:
        def __init__(self):
            self.calls = []

        def ingest(self, selected, _health, _now):
            self.calls.append(selected)
            return TaskRecord(
                task_id=uuid4(),
                task_type="vision_inference",
                idempotency_key=str(selected.frame_sequence),
                payload={},
                status=TaskStatus.READY,
                attempt_count=0,
                created_at=NOW,
            )

    import asyncio

    encoder, saga = Encoder(), Saga()
    report = asyncio.run(
        IngestionService(encoder, clock=lambda: NOW).run(
            Source(),
            organization_id=organization_id,
            health=Health(),
            saga=saga,
            max_frames=10,
        )
    )

    assert (report.sampled, report.encoded, report.admitted) == (2, 2, 2)
    assert [item.frame_sequence for item in saga.calls] == [1, 6]
    assert all(item.content == b"jpeg" for item in saga.calls)


def test_rtsp_source_reconnects_with_capped_backoff():
    from odp_api.adapters.frame_sources.opencv import RtspSource

    captures = iter([FakeCapture([]), FakeCapture([b"frame"])])
    delays = []

    async def sleep(delay):
        delays.append(delay)

    source = RtspSource(
        "rtsp://camera.invalid/stream",
        camera_id=uuid4(),
        session_id=uuid4(),
        capture_factory=lambda _source: next(captures),
        reconnect_delay_seconds=0.1,
        max_reconnect_delay_seconds=0.2,
        sleep=sleep,
    )
    import asyncio

    frame = asyncio.run(anext(source.frames()))
    assert frame.frame == b"frame"
    assert delays == [0.1]


def test_local_camera_requires_explicit_nonnegative_device_index():
    from odp_api.adapters.frame_sources.opencv import LocalCameraSource

    with pytest.raises(ValueError, match="device index"):
        LocalCameraSource(-1, camera_id=uuid4(), session_id=uuid4())


def test_frame_ingestor_uses_database_session_claim_and_sanitizes_credentials():
    from odp_api.processes.frame_ingestor import FrameIngestor, sanitize_source_uri

    assert sanitize_source_uri("rtsp://user:password@camera.invalid:554/live?token=x") == (
        "rtsp://camera.invalid:554/live?token=x"
    )
    session = InspectionSession(
        session_id=uuid4(),
        organization_id=uuid4(),
        camera_id=uuid4(),
        line_id=uuid4(),
        source_type="RECORDED",
        sanitized_uri="/safe/fixture.mp4",
        secret_reference=None,
        status="RUNNING",
    )

    class Sessions:
        def __init__(self):
            self.closed = False

        def claim_start_requests(self, process_id, limit):
            return [session]

        def heartbeat(self, session_id, process_id, now):
            return True

        def claim_stop_requests(self, process_id, limit):
            return []

        def mark_failed(self, *args):
            self.closed = True
            return True

    class Source:
        def __init__(self):
            self.closed = False

        async def frames(self):
            yield DecodedFrame(session.camera_id, session.session_id, 1, NOW, b"raw")

        async def close(self):
            self.closed = True

    class Health:
        def snapshot(self, _organization_id, _camera_id):
            return ArtifactHealth(True, True, 0, 0)

    class Encoder:
        def encode(self, _frame):
            return b"jpeg"

    class Saga:
        def ingest(self, selected, health, now):
            return TaskRecord(
                task_id=uuid4(),
                task_type="vision_inference",
                idempotency_key="1",
                payload={},
                status=TaskStatus.READY,
                attempt_count=0,
                created_at=NOW,
            )

    source = Source()
    sessions = Sessions()
    import asyncio

    report = asyncio.run(
        FrameIngestor(
            sessions,
            IngestionService(Encoder(), clock=lambda: NOW),
            Health(),
            lambda _session: source,
            lambda _session: Saga(),
            process_id="ingestor-1",
        ).run_once(max_frames_per_session=1)
    )
    assert (report.started, report.failed, report.reports[0].admitted) == (1, 0, 1)
    assert source.closed
