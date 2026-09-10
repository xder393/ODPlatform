"""SQLite proofs for generation-fenced frame admission and publication."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
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

        def delete(self, object_key):
            del object_key
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary delete failure")

    storage = RetryDeleteStorage()
    candidates = admission.cleanup_candidates(clock[0], 10)
    assert [candidate.artifact_id for candidate in candidates] == [old_reservation.artifact_id]

    with pytest.raises(OSError, match="temporary delete failure"):
        storage.delete(candidates[0].object_key)
    with sessions() as session:
        assert session.get(FrameArtifactRow, old_reservation.artifact_id).state == ArtifactState.FAILED.value
        assert session.get(CameraInferenceStateRow, (organization_id, camera_id)).reservation_id == new_reservation.reservation_id

    storage.delete(candidates[0].object_key)
    assert admission.mark_artifact_deleted(candidates[0], clock[0])
    with sessions() as session:
        assert session.get(FrameArtifactRow, old_reservation.artifact_id).state == ArtifactState.DELETED.value
        assert session.get(CameraInferenceStateRow, (organization_id, camera_id)).reservation_id == new_reservation.reservation_id


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
