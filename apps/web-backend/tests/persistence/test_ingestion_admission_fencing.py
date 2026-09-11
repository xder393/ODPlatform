"""SQLite proofs for generation-fenced frame admission and publication."""

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
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
from odp_api.modules.ingestion.artifacts import (
    ArtifactHealth,
    ArtifactSaga,
    SelectedFrame,
)
from odp_api.modules.ingestion.reconciler import ArtifactReconciler
from odp_api.modules.tasks.models import ArtifactState
from odp_api.ports.storage import ObjectMetadata
from odp_api.ports.tasks import AdmissionRejected, AdmissionRequest

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


@pytest.fixture
def fenced_repositories(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ingestion-fencing.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    clock = [NOW]
    ownership = SqlAlchemyInspectionSessionRepository(
        sessions,
        lease_duration=timedelta(seconds=5),
        clock=lambda _: clock[0],
    )
    admission = SqlAlchemyTaskControlRepository(sessions, clock=lambda _: clock[0])
    organization_id, camera_id, session_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            InspectionSessionRow(
                session_id=session_id,
                organization_id=organization_id,
                camera_id=camera_id,
                line_id=uuid4(),
                source_type="TEST",
                sanitized_uri="file:///sample",
                status="START_REQUESTED",
                idempotency_key=str(session_id),
                started_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    try:
        yield sessions, clock, ownership, admission, organization_id, camera_id, session_id
    finally:
        engine.dispose()


def test_old_generation_cannot_complete_upload_after_session_reclaim(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    old_instance = uuid4()
    new_instance = uuid4()
    old_claim = ownership.claim_available("old", old_instance, 1)[0].claim
    reservation = admission.reserve(
        AdmissionRequest(
            organization_id=organization_id,
            camera_id=camera_id,
            stream_session_id=session_id,
            frame_sequence=10,
            captured_at=NOW,
            content_sha256="a" * 64,
            correlation_id=uuid4(),
            claim=old_claim,
        ),
        NOW,
    )

    clock[0] = NOW + timedelta(seconds=6)
    replacement = ownership.claim_available("new", new_instance, 1)[0].claim
    assert replacement.generation == old_claim.generation + 1

    with pytest.raises(AdmissionRejected, match="INGESTION_LEASE_LOST"):
        admission.complete_upload(
            reservation.reservation_id,
            organization_id,
            "frames/10.jpg",
            10,
            clock[0],
            claim=old_claim,
        )

    with sessions() as session:
        artifact = session.get(FrameArtifactRow, reservation.artifact_id)
        state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
        assert artifact is not None
        assert artifact.state == ArtifactState.PENDING.value
        assert state.reservation_id == reservation.reservation_id
        assert session.scalar(
            select(InferenceTaskRow).where(InferenceTaskRow.artifact_id == reservation.artifact_id)
        ) is None
        assert session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.task_id.is_not(None))
        ) is None


def test_old_generation_cannot_reserve_after_session_reclaim(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    old_claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    first = AdmissionRequest(
        organization_id=organization_id,
        camera_id=camera_id,
        stream_session_id=session_id,
        frame_sequence=10,
        captured_at=NOW,
        content_sha256="a" * 64,
        correlation_id=uuid4(),
        claim=old_claim,
    )
    reservation = admission.reserve(first, NOW)

    clock[0] = NOW + timedelta(seconds=6)
    replacement = ownership.claim_available("new", uuid4(), 1)[0].claim
    assert replacement.generation == old_claim.generation + 1

    with pytest.raises(AdmissionRejected, match="INGESTION_LEASE_LOST"):
        admission.reserve(replace(first, frame_sequence=11), clock[0])

    with sessions() as session:
        artifacts = session.scalars(select(FrameArtifactRow)).all()
        state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
        inspection = session.get(InspectionSessionRow, session_id)
        assert [artifact.artifact_id for artifact in artifacts] == [reservation.artifact_id]
        assert state.reservation_id == reservation.reservation_id
        assert inspection.last_reserved_sequence == 10


def test_reconciler_cannot_promote_old_generation_after_session_reclaim(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    old_claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    reservation = admission.reserve(
        AdmissionRequest(
            organization_id=organization_id,
            camera_id=camera_id,
            stream_session_id=session_id,
            frame_sequence=10,
            captured_at=NOW,
            content_sha256="a" * 64,
            correlation_id=uuid4(),
            claim=old_claim,
        ),
        NOW,
    )
    candidate = admission.pending_artifacts(NOW, 10)[0]

    clock[0] = NOW + timedelta(seconds=6)
    replacement = ownership.claim_available("new", uuid4(), 1)[0].claim
    assert replacement.generation == old_claim.generation + 1
    metadata = ObjectMetadata(candidate.object_key, 10, "a" * 64)

    assert not admission.reconcile_pending_artifact(candidate, metadata, clock[0])

    with sessions() as session:
        artifact = session.get(FrameArtifactRow, reservation.artifact_id)
        state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
        assert artifact is not None
        assert artifact.state == ArtifactState.FAILED.value
        assert artifact.error_code == "INGESTION_LEASE_LOST"
        assert state.reservation_id is None
        assert session.scalar(
            select(InferenceTaskRow).where(InferenceTaskRow.artifact_id == reservation.artifact_id)
        ) is None
        assert session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.task_id.is_not(None))
        ) is None


def test_old_generation_cannot_fail_upload_after_session_reclaim(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    old_claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    reservation = admission.reserve(
        AdmissionRequest(
            organization_id=organization_id,
            camera_id=camera_id,
            stream_session_id=session_id,
            frame_sequence=10,
            captured_at=NOW,
            content_sha256="a" * 64,
            correlation_id=uuid4(),
            claim=old_claim,
        ),
        NOW,
    )
    clock[0] = NOW + timedelta(seconds=6)
    new_claim = ownership.claim_available("new", uuid4(), 1)[0].claim
    assert new_claim.generation == old_claim.generation + 1

    with pytest.raises(AdmissionRejected, match="INGESTION_LEASE_LOST"):
        admission.fail_upload(
            reservation.reservation_id,
            organization_id,
            "STORAGE_UPLOAD_FAILED",
            clock[0],
            claim=old_claim,
        )

    with sessions() as session:
        artifact = session.get(FrameArtifactRow, reservation.artifact_id)
        state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
        assert artifact.state == ArtifactState.PENDING.value
        assert artifact.error_code is None
        assert state.reservation_id == reservation.reservation_id


def test_failed_object_cleanup_retries_without_releasing_new_reservation(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    old_claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    old_reservation = admission.reserve(
        AdmissionRequest(
            organization_id=organization_id,
            camera_id=camera_id,
            stream_session_id=session_id,
            frame_sequence=10,
            captured_at=NOW,
            content_sha256="a" * 64,
            correlation_id=uuid4(),
            claim=old_claim,
        ),
        NOW,
    )
    old_candidate = admission.pending_artifacts(NOW, 10)[0]

    clock[0] = NOW + timedelta(seconds=6)
    new_claim = ownership.claim_available("new", uuid4(), 1)[0].claim
    assert not admission.reconcile_pending_artifact(
        old_candidate,
        ObjectMetadata(old_candidate.object_key, 10, "a" * 64),
        clock[0],
    )
    new_reservation = admission.reserve(
        AdmissionRequest(
            organization_id=organization_id,
            camera_id=camera_id,
            stream_session_id=session_id,
            frame_sequence=11,
            captured_at=clock[0],
            content_sha256="b" * 64,
            correlation_id=uuid4(),
            claim=new_claim,
        ),
        clock[0],
    )

    class RetryDeleteStorage:
        def __init__(self):
            self.attempts = 0

        def head(self, object_key):
            del object_key
            raise AssertionError("failed ownership candidate must skip HEAD")

        def delete(self, object_key):
            del object_key
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary delete failure")

    storage = RetryDeleteStorage()
    reconciler = ArtifactReconciler(admission, storage)
    # Keep the newer PENDING reservation newer than this recovery snapshot so
    # the reconciler exercises only the old cleanup candidate.
    first = reconciler.run_once(NOW)
    assert first.deleted == 0
    assert storage.attempts == 1
    with sessions() as session:
        assert session.get(FrameArtifactRow, old_reservation.artifact_id).state == ArtifactState.FAILED.value
        assert session.get(CameraInferenceStateRow, (organization_id, camera_id)).reservation_id == new_reservation.reservation_id

    new_key = f"organizations/{organization_id}/artifacts/{new_reservation.artifact_id}"
    admission.complete_upload(
        new_reservation.reservation_id,
        organization_id,
        new_key,
        10,
        clock[0],
        claim=new_claim,
    )
    clock[0] = NOW + timedelta(seconds=37)
    second = reconciler.run_once(clock[0])
    assert second.deleted == 1
    assert storage.attempts == 2
    with sessions() as session:
        assert session.get(FrameArtifactRow, old_reservation.artifact_id).state == ArtifactState.DELETED.value
        assert session.get(FrameArtifactRow, new_reservation.artifact_id).state == ArtifactState.AVAILABLE.value
        assert session.get(CameraInferenceStateRow, (organization_id, camera_id)).reservation_id is None


def test_reserve_rejects_cross_camera_claim(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    with pytest.raises(AdmissionRejected, match="INGESTION_LEASE_LOST"):
        admission.reserve(
            AdmissionRequest(
                organization_id=organization_id,
                camera_id=camera_id,
                stream_session_id=session_id,
                frame_sequence=10,
                captured_at=clock[0],
                content_sha256="a" * 64,
                correlation_id=uuid4(),
                claim=replace(claim, camera_id=uuid4()),
            ),
            clock[0],
        )
    with sessions() as session:
        assert session.scalar(select(FrameArtifactRow)) is None


def test_reserve_rejects_cross_tenant_claim(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    with pytest.raises(AdmissionRejected, match="INGESTION_LEASE_LOST"):
        admission.reserve(
            AdmissionRequest(
                organization_id=organization_id,
                camera_id=camera_id,
                stream_session_id=session_id,
                frame_sequence=10,
                captured_at=clock[0],
                content_sha256="a" * 64,
                correlation_id=uuid4(),
                claim=replace(claim, organization_id=uuid4()),
            ),
            clock[0],
        )
    with sessions() as session:
        assert session.scalar(select(FrameArtifactRow)) is None


def test_reserve_rejects_claim_after_session_stopped(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    with sessions.begin() as session:
        session.get(InspectionSessionRow, session_id).status = "STOPPED"
    with pytest.raises(AdmissionRejected, match="INGESTION_LEASE_LOST"):
        admission.reserve(
            AdmissionRequest(
                organization_id=organization_id,
                camera_id=camera_id,
                stream_session_id=session_id,
                frame_sequence=10,
                captured_at=clock[0],
                content_sha256="a" * 64,
                correlation_id=uuid4(),
                claim=claim,
            ),
            clock[0],
        )


def test_high_water_rejects_reused_sequence_but_allows_same_content_later(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    request = AdmissionRequest(
        organization_id=organization_id,
        camera_id=camera_id,
        stream_session_id=session_id,
        frame_sequence=10,
        captured_at=clock[0],
        content_sha256="a" * 64,
        correlation_id=uuid4(),
        claim=claim,
    )
    reservation = admission.reserve(request, clock[0])
    admission.fail_upload(
        reservation.reservation_id,
        organization_id,
        "STORAGE_UPLOAD_FAILED",
        clock[0],
        claim=claim,
    )

    with pytest.raises(AdmissionRejected, match="FRAME_ALREADY_ADMITTED"):
        admission.reserve(request, clock[0])
    later = admission.reserve(replace(request, frame_sequence=11), clock[0])
    assert later.frame_sequence == 11
    admission.fail_upload(
        later.reservation_id,
        organization_id,
        "STORAGE_UPLOAD_FAILED",
        clock[0],
        claim=claim,
    )

    with sessions.begin() as session:
        session.delete(session.get(FrameArtifactRow, reservation.artifact_id))
    with pytest.raises(AdmissionRejected, match="FRAME_SEQUENCE_NOT_ADVANCED"):
        admission.reserve(request, clock[0])
    with sessions() as session:
        assert session.get(InspectionSessionRow, session_id).last_reserved_sequence == 11


def test_same_sequence_different_content_is_rejected(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    request = AdmissionRequest(
        organization_id=organization_id,
        camera_id=camera_id,
        stream_session_id=session_id,
        frame_sequence=10,
        captured_at=clock[0],
        content_sha256="a" * 64,
        correlation_id=uuid4(),
        claim=claim,
    )
    reservation = admission.reserve(request, clock[0])

    with pytest.raises(AdmissionRejected, match="FRAME_ALREADY_ADMITTED"):
        admission.reserve(replace(request, content_sha256="b" * 64), clock[0])

    with sessions() as session:
        artifacts = session.scalars(select(FrameArtifactRow)).all()
        assert [artifact.artifact_id for artifact in artifacts] == [reservation.artifact_id]
        assert session.get(InspectionSessionRow, session_id).last_reserved_sequence == 10


def test_failed_reserve_transaction_does_not_advance_high_water(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    request = AdmissionRequest(
        organization_id=organization_id,
        camera_id=camera_id,
        stream_session_id=session_id,
        frame_sequence=10,
        captured_at=clock[0],
        content_sha256="a" * 64,
        correlation_id=uuid4(),
        claim=claim,
    )

    def fail_flush(_session, _flush_context):
        raise RuntimeError("forced reserve transaction failure")

    event.listen(sessions.class_, "after_flush", fail_flush)
    try:
        with pytest.raises(RuntimeError, match="forced reserve transaction failure"):
            admission.reserve(request, clock[0])
    finally:
        event.remove(sessions.class_, "after_flush", fail_flush)

    with sessions() as session:
        assert session.scalar(select(FrameArtifactRow)) is None
        assert session.get(InspectionSessionRow, session_id).last_reserved_sequence == 0


def test_saga_failed_upload_cleanup_retries_through_reconciler(fenced_repositories):
    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    content = b"jpeg-bytes"
    digest = sha256(content).hexdigest()

    class DeleteRetryStorage:
        def __init__(self):
            self.objects = {}
            self.delete_attempts = 0

        def put(self, object_key, value, *, sha256, content_type="application/octet-stream"):
            del content_type
            self.objects[object_key] = ObjectMetadata(object_key, len(value), sha256)

        def head(self, object_key):
            stored = self.objects.get(object_key)
            if stored is None:
                return None
            return ObjectMetadata(object_key, stored.content_length + 1, stored.sha256)

        def delete(self, object_key):
            self.delete_attempts += 1
            if self.delete_attempts < 3:
                raise OSError("temporary delete failure")
            self.objects.pop(object_key, None)

    storage = DeleteRetryStorage()
    saga = ArtifactSaga(admission, storage)
    selected = SelectedFrame(
        organization_id=organization_id,
        camera_id=camera_id,
        stream_session_id=session_id,
        frame_sequence=10,
        captured_at=clock[0],
        content=content,
        correlation_id=uuid4(),
        claim=claim,
    )

    result = saga.ingest(
        selected,
        ArtifactHealth(worker_healthy=True, redis_available=True),
        clock[0],
    )

    assert isinstance(result, AdmissionRejected)
    assert result.reason == "STORAGE_UPLOAD_FAILED"
    assert storage.delete_attempts == 1
    with sessions() as session:
        artifact = session.scalar(select(FrameArtifactRow))
        assert artifact.state == ArtifactState.FAILED.value
        assert artifact.lifecycle == "PROCESSING"
        assert artifact.error_code == "STORAGE_UPLOAD_FAILED"
        assert artifact.sha256 == digest

    reconciler = ArtifactReconciler(admission, storage)
    first = reconciler.run_once(clock[0])
    assert first.deleted == 0
    assert storage.delete_attempts == 2
    with sessions() as session:
        assert session.scalar(select(FrameArtifactRow)).state == ArtifactState.FAILED.value

    clock[0] = clock[0] + timedelta(seconds=31)
    second = reconciler.run_once(clock[0])
    assert second.deleted == 1
    assert storage.delete_attempts == 3
    with sessions() as session:
        assert session.scalar(select(FrameArtifactRow)).state == ArtifactState.DELETED.value


def test_expired_reservation_late_put_is_revisited_by_real_reconciler(fenced_repositories):
    """A cleanup tombstone must catch an uncancellable PUT that finishes later."""

    sessions, clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    old_claim = ownership.claim_available("old", uuid4(), 1)[0].claim

    class BlockingStorage:
        def __init__(self):
            self.put_entered = threading.Event()
            self.release_put = threading.Event()
            self.objects = {}
            self.delete_calls = []

        def put(self, object_key, content, *, sha256, content_type="application/octet-stream"):
            del content_type
            self.put_entered.set()
            assert self.release_put.wait(2)
            metadata = ObjectMetadata(object_key, len(content), sha256)
            self.objects[object_key] = metadata
            return metadata

        def head(self, object_key):
            return self.objects.get(object_key)

        def delete(self, object_key):
            self.delete_calls.append(object_key)
            self.objects.pop(object_key, None)

    storage = BlockingStorage()
    saga = ArtifactSaga(admission, storage)
    old_content = b"old-frame"
    old_digest = sha256(old_content).hexdigest()
    old_selected = SelectedFrame(
        organization_id=organization_id,
        camera_id=camera_id,
        stream_session_id=session_id,
        frame_sequence=10,
        captured_at=NOW,
        content=old_content,
        correlation_id=uuid4(),
        claim=old_claim,
    )
    old_result = []
    old_worker = threading.Thread(
        target=lambda: old_result.append(
            saga.ingest(
                old_selected,
                ArtifactHealth(worker_healthy=True, redis_available=True),
                NOW,
            )
        )
    )
    old_worker.start()
    assert storage.put_entered.wait(2)
    old_candidate = admission.pending_artifacts(NOW, 10)[0]

    clock[0] = NOW + timedelta(seconds=31)
    new_claim = ownership.claim_available("new", uuid4(), 1)[0].claim
    new_reservation = admission.reserve(
        AdmissionRequest(
            organization_id=organization_id,
            camera_id=camera_id,
            stream_session_id=session_id,
            frame_sequence=11,
            captured_at=clock[0],
            content_sha256=sha256(b"new-frame").hexdigest(),
            correlation_id=uuid4(),
            claim=new_claim,
        ),
        clock[0],
    )
    old_object_key = old_candidate.object_key

    reconciler = ArtifactReconciler(admission, storage)
    first = reconciler.run_once(NOW)
    assert first.deleted == 1

    storage.release_put.set()
    old_worker.join(2)
    assert not old_worker.is_alive()
    assert isinstance(old_result[0], AdmissionRejected)
    assert storage.objects[old_object_key].sha256 == old_digest

    new_content = b"new-frame"
    new_key = f"organizations/{organization_id}/artifacts/{new_reservation.artifact_id}"
    storage.objects[new_key] = ObjectMetadata(
        new_key,
        len(new_content),
        sha256(new_content).hexdigest(),
    )
    admission.complete_upload(
        new_reservation.reservation_id,
        organization_id,
        new_key,
        len(new_content),
        clock[0],
        claim=new_claim,
    )

    clock[0] = NOW + timedelta(seconds=90)
    second = reconciler.run_once(clock[0])
    assert second.deleted == 1
    assert old_object_key not in storage.objects
    assert new_key in storage.objects

    with sessions() as session:
        old_artifact = session.get(FrameArtifactRow, old_candidate.artifact_id)
        new_artifact = session.get(FrameArtifactRow, new_reservation.artifact_id)
        assert old_artifact.state == ArtifactState.DELETED.value
        assert old_artifact.cleanup_next_attempt_at is not None
        assert old_artifact.cleanup_next_attempt_at.replace(tzinfo=UTC) > clock[0]
        assert new_artifact.state == ArtifactState.AVAILABLE.value
        assert session.scalar(
            select(InferenceTaskRow).where(
                InferenceTaskRow.artifact_id == old_candidate.artifact_id
            )
        ) is None
        new_task = session.scalar(
            select(InferenceTaskRow).where(
                InferenceTaskRow.artifact_id == new_reservation.artifact_id
            )
        )
        assert new_task is not None
        assert session.scalar(
            select(OutboxEventRow).where(
                OutboxEventRow.task_id == new_task.task_id
            )
        ) is not None


def test_cleanup_failure_cooldown_keeps_later_candidate_fair(fenced_repositories):
    sessions, _clock, ownership, admission, organization_id, camera_id, session_id = (
        fenced_repositories
    )
    claim = ownership.claim_available("old", uuid4(), 1)[0].claim
    for frame_sequence in (10, 11):
        reservation = admission.reserve(
            AdmissionRequest(
                organization_id=organization_id,
                camera_id=camera_id,
                stream_session_id=session_id,
                frame_sequence=frame_sequence,
                captured_at=NOW,
                content_sha256=("a" if frame_sequence == 10 else "b") * 64,
                correlation_id=uuid4(),
                claim=claim,
            ),
            NOW,
        )
        admission.fail_upload(
            reservation.reservation_id,
            organization_id,
            "STORAGE_UPLOAD_FAILED",
            NOW,
            claim=claim,
        )

    candidates = admission.cleanup_candidates(NOW, 10)
    assert len(candidates) == 2
    first, second = candidates

    class FairStorage:
        def __init__(self):
            self.delete_calls = []

        def delete(self, object_key):
            self.delete_calls.append(object_key)
            if object_key == first.object_key:
                raise OSError("first candidate temporarily unavailable")

    storage = FairStorage()
    reconciler = ArtifactReconciler(admission, storage)
    assert reconciler.run_once(NOW, limit=1).deleted == 0
    with sessions() as session:
        first_row = session.get(FrameArtifactRow, first.artifact_id)
        assert first_row.cleanup_next_attempt_at is not None
        assert first_row.cleanup_next_attempt_at.replace(tzinfo=UTC) > NOW
    assert reconciler.run_once(NOW, limit=1).deleted == 1
    assert storage.delete_calls == [first.object_key, second.object_key]

    with sessions() as session:
        assert session.get(FrameArtifactRow, first.artifact_id).state == ArtifactState.FAILED.value
        assert session.get(FrameArtifactRow, second.artifact_id).state == ArtifactState.DELETED.value
