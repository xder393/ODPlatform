"""Runtime contracts for resumable ingestion sessions."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from odp_api.adapters.persistence.inspection_sessions import (
    SqlAlchemyInspectionSessionRepository,
)
from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
)
from odp_api.modules.ingestion.artifacts import ArtifactHealth, ArtifactSaga
from odp_api.modules.ingestion.service import IngestionService
from odp_api.ports.frame_sources import DecodedFrame
from odp_api.ports.inspection_sessions import (
    ClaimedInspectionSession,
    IngestionClaim,
    InspectionSession,
)
from odp_api.ports.storage import ObjectMetadata
from odp_api.ports.tasks import AdmissionRequest
from odp_api.processes import runtime
from odp_api.processes.frame_ingestor import FrameIngestor
from odp_api.settings import IngestorSettings

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def _claimed(*, initial_sequence: int = 9) -> ClaimedInspectionSession:
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
    claim = IngestionClaim(
        session.organization_id,
        session.camera_id,
        session.session_id,
        uuid4(),
        2,
    )
    return ClaimedInspectionSession(session, claim, initial_sequence)


def test_ingestor_settings_use_dedicated_recovery_intervals():
    settings = IngestorSettings.valid_test_instance()

    assert settings.ingestion_heartbeat_seconds == 5
    assert settings.ingestion_lease_seconds == 30
    assert settings.ingestion_poll_seconds == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ingestion_heartbeat_seconds", 0),
        ("ingestion_heartbeat_seconds", float("nan")),
        ("ingestion_poll_seconds", float("inf")),
        ("ingestion_lease_seconds", True),
        ("ingestion_poll_seconds", "not-a-number"),
    ],
)
def test_ingestor_settings_reject_invalid_recovery_intervals(field, value):
    with pytest.raises(ValidationError):
        IngestorSettings(**{field: value})


def test_ingestor_settings_require_three_heartbeat_intervals_per_lease():
    with pytest.raises(ValidationError, match="three heartbeat"):
        IngestorSettings(
            ingestion_heartbeat_seconds=5,
            ingestion_lease_seconds=14,
        )


def test_claimed_source_factory_resumes_from_persisted_sequence():
    claimed = _claimed(initial_sequence=9)

    source = runtime.source_from_session(claimed)

    assert source._sequence == 9


def test_source_factory_requires_claimed_inspection_session_envelope():
    session = SimpleNamespace(
        source_type="RECORDED",
        sanitized_uri="/safe/fixture.mp4",
        camera_id=uuid4(),
        session_id=uuid4(),
    )

    with pytest.raises(TypeError, match="ClaimedInspectionSession"):
        runtime.source_from_session(session)


def test_source_factory_keeps_unknown_source_types_rejected():
    claimed = _claimed()
    session = InspectionSession(
        session_id=claimed.session.session_id,
        organization_id=claimed.session.organization_id,
        camera_id=claimed.session.camera_id,
        line_id=claimed.session.line_id,
        source_type="USB",
        sanitized_uri="/dev/video0",
        secret_reference=None,
        status="RUNNING",
    )

    with pytest.raises(ValueError, match="unsupported inspection source type"):
        runtime.source_from_session(
            ClaimedInspectionSession(session, claimed.claim, claimed.initial_sequence)
        )


def test_cancelled_old_upload_cannot_publish_or_release_new_reservation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'delayed-upload.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    clock = [NOW]
    ownership = SqlAlchemyInspectionSessionRepository(
        sessions,
        lease_duration=timedelta(seconds=5),
        clock=lambda _session: clock[0],
    )
    admission = SqlAlchemyTaskControlRepository(sessions, clock=lambda _session: clock[0])
    organization_id, camera_id, session_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            InspectionSessionRow(
                session_id=session_id,
                organization_id=organization_id,
                camera_id=camera_id,
                line_id=uuid4(),
                source_type="RECORDED",
                sanitized_uri="/safe/fixture.mp4",
                status="START_REQUESTED",
                idempotency_key=str(session_id),
                created_at=NOW,
                updated_at=NOW,
            )
        )

    class DelayedStorage:
        def __init__(self):
            self.entered = threading.Event()
            self.unblock = threading.Event()
            self.head_called = threading.Event()
            self.objects = {}

        def put(self, object_key, content, *, sha256, content_type="application/octet-stream"):
            del content_type
            self.entered.set()
            assert self.unblock.wait(5)
            metadata = ObjectMetadata(object_key, len(content), sha256)
            self.objects[object_key] = (bytes(content), metadata)
            return metadata

        def head(self, object_key):
            self.head_called.set()
            return self.objects.get(object_key, (None, None))[1]

        def delete(self, object_key):
            self.objects.pop(object_key, None)

        def get(self, object_key):
            return self.objects[object_key][0]

        def presign_get(self, object_key, expires_seconds=60):
            del object_key, expires_seconds
            return "unused"

    class Source:
        async def frames(self):
            yield DecodedFrame(camera_id, session_id, 1, NOW, b"decoded")

        async def close(self):
            return None

    class Encoder:
        def encode(self, _frame):
            return b"jpeg"

    class Health:
        def snapshot(self, _organization_id, _camera_id):
            return ArtifactHealth(True, True)

    storage = DelayedStorage()
    complete_called = threading.Event()
    real_complete_upload = admission.complete_upload

    def complete_upload(*args, **kwargs):
        try:
            return real_complete_upload(*args, **kwargs)
        finally:
            complete_called.set()

    admission.complete_upload = complete_upload
    old_instance, new_instance = uuid4(), uuid4()
    old_claimed = ownership.claim_available("old", old_instance, 1)[0]
    source = Source()
    process = FrameIngestor(
        ownership,
        IngestionService(Encoder(), clock=lambda: NOW),
        Health(),
        lambda _claimed: source,
        lambda _session: ArtifactSaga(admission, storage),
        process_id="old",
        instance_id=old_instance,
        heartbeat_interval_seconds=60,
    )
    active = {}

    async def scenario():
        active[old_claimed.claim] = asyncio.create_task(
            process._run_claimed(old_claimed, None)
        )
        assert await asyncio.to_thread(storage.entered.wait, 2)
        await process._shutdown_active(active)
        assert not active

        clock[0] = NOW + timedelta(seconds=31)
        replacement = ownership.claim_available("new", new_instance, 1)[0]
        assert replacement.claim.generation == old_claimed.claim.generation + 1
        new_reservation = admission.reserve(
            AdmissionRequest(
                organization_id=organization_id,
                camera_id=camera_id,
                stream_session_id=session_id,
                frame_sequence=2,
                captured_at=NOW,
                content_sha256=sha256(b"new").hexdigest(),
                correlation_id=uuid4(),
                claim=replacement.claim,
            ),
            NOW,
        )

        storage.unblock.set()
        assert await asyncio.to_thread(storage.head_called.wait, 2)
        assert await asyncio.to_thread(complete_called.wait, 2)
        with sessions() as session:
            old_artifact = session.scalar(
                select(FrameArtifactRow).where(
                    FrameArtifactRow.frame_sequence == 1,
                )
            )
            new_artifact = session.scalar(
                select(FrameArtifactRow).where(
                    FrameArtifactRow.frame_sequence == 2,
                )
            )
            state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
            inspection = session.get(InspectionSessionRow, session_id)
            assert old_artifact.state == "FAILED"
            assert old_artifact.error_code == "ADMISSION_RESERVATION_EXPIRED"
            assert new_artifact.state == "PENDING"
            assert state.reservation_id == new_reservation.reservation_id
            assert inspection.last_reserved_sequence == 2
            assert session.scalar(select(InferenceTaskRow)) is None
            assert session.scalar(select(OutboxEventRow)) is None

    try:
        asyncio.run(scenario())
    finally:
        storage.unblock.set()
        engine.dispose()
