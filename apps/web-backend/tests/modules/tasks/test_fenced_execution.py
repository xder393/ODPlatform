"""Behavioral contract for fenced worker ownership."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.tasks.models import TaskStatus
from odp_api.ports.tasks import StaleLease, TaskExecutionPort
from sqlalchemy import select


def test_execution_port_exposes_fenced_mutations():
    assert StaleLease
    assert TaskExecutionPort


def test_duplicate_delivery_creates_one_attempt_and_tenant_guard_fails_closed(tmp_path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'fencing.db'}")
    Base.metadata.create_all(engine)
    org, camera, artifact, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    with sessions.begin() as s:
        s.add_all([
            FrameArtifactRow(artifact_id=artifact, organization_id=org, camera_id=camera, stream_session_id=uuid4(), frame_sequence=1, captured_at=now, sha256='a'*64, state='AVAILABLE', lifecycle='PROCESSING', created_at=now, updated_at=now),
            InferenceTaskRow(task_id=task_id, organization_id=org, camera_id=camera, artifact_id=artifact, idempotency_key='k', status=TaskStatus.READY.value, dispatch_seq=1, attempt_count=0, fence_token=0, created_at=now, updated_at=now),
            CameraInferenceStateRow(organization_id=org, camera_id=camera, running_task_id=None, ready_count=1, version=0),
        ])
    repo = SqlAlchemyTaskControlRepository(sessions)
    assert repo.claim(task_id, org, 'a', now) is not None
    assert repo.claim(task_id, org, 'b', now) is None
    assert repo.claim(task_id, uuid4(), 'c', now) is None
    with sessions() as s:
        assert s.scalar(select(InferenceAttemptRow).where(InferenceAttemptRow.task_id == task_id)) is not None
        assert len(s.scalars(select(InferenceAttemptRow).where(InferenceAttemptRow.task_id == task_id)).all()) == 1
    engine.dispose()


def test_renewal_preserves_token_and_rejects_wrong_owner_or_tenant(tmp_path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'renew.db'}")
    Base.metadata.create_all(engine)
    org, camera, artifact, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    with sessions.begin() as s:
        s.add_all([FrameArtifactRow(artifact_id=artifact, organization_id=org, camera_id=camera, stream_session_id=uuid4(), frame_sequence=1, captured_at=now, sha256='a'*64, state='AVAILABLE', lifecycle='PROCESSING', created_at=now, updated_at=now), InferenceTaskRow(task_id=task_id, organization_id=org, camera_id=camera, artifact_id=artifact, idempotency_key='k', status='READY', dispatch_seq=1, attempt_count=0, fence_token=0, created_at=now, updated_at=now), CameraInferenceStateRow(organization_id=org, camera_id=camera, running_task_id=None, ready_count=1, version=0)])
    repo = SqlAlchemyTaskControlRepository(sessions)
    claim = repo.claim(task_id, org, 'owner', now)
    assert claim is not None
    renewed = repo.renew(claim, now + timedelta(seconds=5))
    assert renewed is not None and renewed.fence_token == claim.fence_token and renewed.lease_expires_at >= claim.lease_expires_at
    from dataclasses import replace
    assert repo.renew(replace(claim, lease_owner='other'), now) is None
    assert repo.renew(replace(claim, organization_id=uuid4()), now) is None
    engine.dispose()
