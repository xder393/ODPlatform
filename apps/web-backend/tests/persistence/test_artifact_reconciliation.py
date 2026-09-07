"""Database-side fencing for artifact orphan recovery and cleanup."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
)
from odp_api.modules.ingestion.reconciler import PendingArtifact
from odp_api.modules.tasks.models import ArtifactState
from odp_api.ports.storage import ObjectMetadata

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'artifacts.db'}")
    Base.metadata.create_all(engine)
    yield SqlAlchemyTaskControlRepository(sessionmaker(bind=engine), clock=lambda _: NOW)
    engine.dispose()


def _seed_pending(repository, *, expires_at):
    organization_id, camera_id, artifact_id, session_id = (uuid4() for _ in range(4))
    with repository._session_factory() as session:
        session.add_all(
            [
                InspectionSessionRow(
                    session_id=session_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    line_id=uuid4(),
                    source_type="FILE",
                    sanitized_uri="file:///sample",
                    status="RUNNING",
                    idempotency_key=str(session_id),
                    started_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                FrameArtifactRow(
                    artifact_id=artifact_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    stream_session_id=session_id,
                    frame_sequence=1,
                    captured_at=NOW,
                    object_key=None,
                    sha256="a" * 64,
                    content_length=None,
                    state=ArtifactState.PENDING.value,
                    lifecycle="PROCESSING",
                    retention_until=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CameraInferenceStateRow(
                    organization_id=organization_id,
                    camera_id=camera_id,
                    running_task_id=None,
                    ready_count=0,
                    reservation_id=artifact_id,
                    reservation_expires_at=expires_at,
                    last_admitted_at=None,
                    version=1,
                    updated_at=NOW,
                ),
            ]
        )
        session.commit()
    return organization_id, camera_id, artifact_id


def _candidate(artifact_id, organization_id):
    return PendingArtifact(
        artifact_id=artifact_id,
        organization_id=organization_id,
        object_key=f"organizations/{organization_id}/artifacts/{artifact_id}",
        sha256="a" * 64,
        content_length=None,
    )


def test_pending_artifact_reconciles_under_camera_lock(repository):
    organization_id, camera_id, artifact_id = _seed_pending(
        repository, expires_at=NOW + timedelta(seconds=10)
    )
    candidate = repository.pending_artifacts(NOW, 10)[0]
    assert candidate.object_key == f"organizations/{organization_id}/artifacts/{artifact_id}"
    assert repository.reconcile_pending_artifact(
        candidate,
        ObjectMetadata(candidate.object_key, 4, "a" * 64),
        NOW,
    )

    with repository._session_factory() as session:
        artifact = session.get(FrameArtifactRow, artifact_id)
        state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
        assert artifact.state == ArtifactState.AVAILABLE.value
        assert artifact.content_length == 4
        assert state.reservation_id is None
        assert session.scalar(select(InferenceTaskRow).where(InferenceTaskRow.artifact_id == artifact_id))
        assert session.scalar(select(OutboxEventRow).where(OutboxEventRow.task_id.is_not(None)))


def test_expired_pending_artifact_is_failed_and_never_revived(repository):
    organization_id, camera_id, artifact_id = _seed_pending(
        repository, expires_at=NOW - timedelta(seconds=1)
    )
    candidate = repository.pending_artifacts(NOW, 10)[0]
    assert not repository.reconcile_pending_artifact(
        candidate,
        ObjectMetadata(candidate.object_key, 4, "a" * 64),
        NOW,
    )

    with repository._session_factory() as session:
        artifact = session.get(FrameArtifactRow, artifact_id)
        state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
        assert artifact.state == ArtifactState.FAILED.value
        assert artifact.error_code == "ADMISSION_RESERVATION_EXPIRED"
        assert state.reservation_id is None
        assert session.scalar(select(InferenceTaskRow).where(InferenceTaskRow.artifact_id == artifact_id)) is None


def test_cleanup_candidate_rechecks_evidence_reference(repository):
    organization_id, camera_id, artifact_id = _seed_pending(
        repository, expires_at=NOW + timedelta(seconds=10)
    )
    with repository._session_factory() as session:
        artifact = session.get(FrameArtifactRow, artifact_id)
        artifact.state = ArtifactState.AVAILABLE.value
        artifact.lifecycle = "EVIDENCE"
        artifact.object_key = f"organizations/{organization_id}/artifacts/{artifact_id}"
        artifact.content_length = 4
        artifact.retention_until = NOW - timedelta(seconds=1)
        session.get(CameraInferenceStateRow, (organization_id, camera_id)).reservation_id = None
        session.commit()

    candidates = repository.cleanup_candidates(NOW, 10)
    assert len(candidates) == 1
    with repository._session_factory() as session:
        from odp_api.adapters.persistence.models import InspectionEventRow

        session.add(
            InspectionEventRow(
                event_id=uuid4(),
                case_id=uuid4(),
                organization_id=organization_id,
                camera_id=camera_id,
                occurred_at=NOW,
                defect_class="scratch",
                confidence=0.9,
                model_release="test",
                preprocessing_parameters=[],
                threshold=0.5,
                input_frame_sha256="a" * 64,
                line_id=None,
                source_result_id=None,
                evidence_artifact_id=artifact_id,
            )
        )
        session.commit()

    assert not repository.mark_artifact_deleted(candidates[0], NOW)
    with repository._session_factory() as session:
        assert session.get(FrameArtifactRow, artifact_id).state == ArtifactState.AVAILABLE.value
