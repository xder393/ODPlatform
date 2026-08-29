"""Behavioral contract for fenced worker ownership."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
    PublishedInferenceResultRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.tasks.commands import (
    InferenceExecutionContract,
    PublishInferenceCommand,
)
from odp_api.modules.tasks.models import TaskStatus
from odp_api.ports.tasks import StaleLease, TaskExecutionPort


def test_execution_port_exposes_fenced_mutations():
    assert StaleLease
    assert TaskExecutionPort
    assert TaskExecutionPort.publish_success


def test_duplicate_delivery_creates_one_attempt_and_tenant_guard_fails_closed(tmp_path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'fencing.db'}")
    Base.metadata.create_all(engine)
    org, camera, artifact, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    with sessions.begin() as s:
        s.add_all(
            [
                FrameArtifactRow(
                    artifact_id=artifact,
                    organization_id=org,
                    camera_id=camera,
                    stream_session_id=uuid4(),
                    frame_sequence=1,
                    captured_at=now,
                    sha256="a" * 64,
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=task_id,
                    organization_id=org,
                    camera_id=camera,
                    artifact_id=artifact,
                    idempotency_key="k",
                    status=TaskStatus.READY.value,
                    dispatch_seq=1,
                    attempt_count=0,
                    fence_token=0,
                    created_at=now,
                    updated_at=now,
                ),
                CameraInferenceStateRow(
                    organization_id=org,
                    camera_id=camera,
                    running_task_id=None,
                    ready_count=1,
                    version=0,
                ),
            ]
        )
    repo = SqlAlchemyTaskControlRepository(sessions)
    assert repo.claim(task_id, org, "a", now) is not None
    assert repo.claim(task_id, org, "b", now) is None
    assert repo.claim(task_id, uuid4(), "c", now) is None
    with sessions() as s:
        assert (
            s.scalar(
                select(InferenceAttemptRow).where(
                    InferenceAttemptRow.task_id == task_id
                )
            )
            is not None
        )
        assert (
            len(
                s.scalars(
                    select(InferenceAttemptRow).where(
                        InferenceAttemptRow.task_id == task_id
                    )
                ).all()
            )
            == 1
        )
    engine.dispose()


def test_renewal_preserves_token_and_rejects_wrong_owner_or_tenant(tmp_path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'renew.db'}")
    Base.metadata.create_all(engine)
    org, camera, artifact, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    with sessions.begin() as s:
        s.add_all(
            [
                FrameArtifactRow(
                    artifact_id=artifact,
                    organization_id=org,
                    camera_id=camera,
                    stream_session_id=uuid4(),
                    frame_sequence=1,
                    captured_at=now,
                    sha256="a" * 64,
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=task_id,
                    organization_id=org,
                    camera_id=camera,
                    artifact_id=artifact,
                    idempotency_key="k",
                    status="READY",
                    dispatch_seq=1,
                    attempt_count=0,
                    fence_token=0,
                    created_at=now,
                    updated_at=now,
                ),
                CameraInferenceStateRow(
                    organization_id=org,
                    camera_id=camera,
                    running_task_id=None,
                    ready_count=1,
                    version=0,
                ),
            ]
        )
    repo = SqlAlchemyTaskControlRepository(sessions)
    claim = repo.claim(task_id, org, "owner", now)
    assert claim is not None
    renewed = repo.renew(claim, now + timedelta(seconds=5))
    assert (
        renewed is not None
        and renewed.fence_token == claim.fence_token
        and renewed.lease_expires_at >= claim.lease_expires_at
    )
    from dataclasses import replace

    assert repo.renew(replace(claim, lease_owner="other"), now) is None
    assert repo.renew(replace(claim, organization_id=uuid4()), now) is None
    engine.dispose()


def _running_repo(tmp_path):
    engine, sessions = create_engine_and_session(
        f"sqlite:///{tmp_path / 'finalize.db'}"
    )
    Base.metadata.create_all(engine)
    org, camera, artifact, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    with sessions.begin() as s:
        s.add_all(
            [
                FrameArtifactRow(
                    artifact_id=artifact,
                    organization_id=org,
                    camera_id=camera,
                    stream_session_id=uuid4(),
                    frame_sequence=1,
                    captured_at=now,
                    sha256="a" * 64,
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=task_id,
                    organization_id=org,
                    camera_id=camera,
                    artifact_id=artifact,
                    idempotency_key="k",
                    status="READY",
                    dispatch_seq=1,
                    attempt_count=0,
                    fence_token=0,
                    created_at=now,
                    updated_at=now,
                ),
                CameraInferenceStateRow(
                    organization_id=org,
                    camera_id=camera,
                    running_task_id=None,
                    ready_count=1,
                    version=0,
                ),
            ]
        )
    repo = SqlAlchemyTaskControlRepository(sessions)
    claim = repo.claim(task_id, org, "owner", now)
    assert claim is not None
    return engine, sessions, repo, org, task_id, claim, now


def _command(claim, now):
    contract = InferenceExecutionContract(
        "m",
        "b" * 64,
        "1",
        "cpu",
        (1, 3, 4, 4),
        "pre",
        "post",
        0.5,
        0.5,
        "hard",
        False,
        "classes",
    )
    return PublishInferenceCommand(claim, contract, "a" * 64, (), (), uuid4(), now)


def test_complete_no_defect_finishes_attempt_clears_camera_without_publishing(tmp_path):
    engine, sessions, repo, org, task_id, claim, now = _running_repo(tmp_path)
    repo.complete_no_defect(_command(claim, now))
    with sessions() as s:
        task = s.get(InferenceTaskRow, task_id)
        attempt = s.get(InferenceAttemptRow, claim.attempt_id)
        state = s.get(CameraInferenceStateRow, (org, task.camera_id))
        assert task.status == "SUCCEEDED" and attempt.finished_at is not None
        assert state.running_task_id is None
        assert s.scalars(select(PublishedInferenceResultRow)).all() == []
    engine.dispose()


def test_record_failure_finishes_attempt_clears_camera_and_waits_for_retry(tmp_path):
    engine, sessions, repo, org, task_id, claim, now = _running_repo(tmp_path)
    repo.record_failure(claim, "boom", now)
    with sessions() as s:
        task = s.get(InferenceTaskRow, task_id)
        attempt = s.get(InferenceAttemptRow, claim.attempt_id)
        state = s.get(CameraInferenceStateRow, (org, task.camera_id))
        assert task.status == "RETRY_WAIT" and task.error_detail == "boom"
        assert attempt.finished_at is not None and state.running_task_id is None
    engine.dispose()


def test_expired_publish_success_is_stale_and_has_no_result(tmp_path):
    engine, sessions, repo, org, task_id, claim, now = _running_repo(tmp_path)
    with sessions.begin() as s:
        s.get(InferenceTaskRow, task_id).lease_expires_at = now - timedelta(seconds=1)
    try:
        repo.publish_success(_command(claim, now))
    except StaleLease:
        pass
    else:
        raise AssertionError("expired lease must be stale")
    with sessions() as s:
        assert s.scalars(select(PublishedInferenceResultRow)).all() == []
        task = s.get(InferenceTaskRow, task_id)
        attempt = s.get(InferenceAttemptRow, claim.attempt_id)
        state = s.get(CameraInferenceStateRow, (org, task.camera_id))
        assert task.status == TaskStatus.RUNNING.value
        assert attempt.finished_at is None
        assert state.running_task_id == task_id
    engine.dispose()


def test_wrong_tenant_owner_or_token_finalize_fails_closed(tmp_path):
    from dataclasses import replace

    engine, sessions, repo, org, task_id, claim, now = _running_repo(tmp_path)
    for bad in (
        replace(claim, organization_id=uuid4()),
        replace(claim, lease_owner="other"),
        replace(claim, fence_token=claim.fence_token + 1),
    ):
        try:
            repo.publish_success(_command(bad, now))
        except StaleLease:
            pass
        else:
            raise AssertionError("invalid claim must be stale")
    with sessions() as s:
        assert s.scalars(select(PublishedInferenceResultRow)).all() == []
        task = s.get(InferenceTaskRow, task_id)
        attempt = s.get(InferenceAttemptRow, claim.attempt_id)
        state = s.get(CameraInferenceStateRow, (org, task.camera_id))
        assert task.status == TaskStatus.RUNNING.value
        assert attempt.finished_at is None
        assert state.running_task_id == task_id
    engine.dispose()


def test_expired_same_task_is_reclaimed_with_new_fence_and_attempt(tmp_path):
    engine, sessions, repo, org, task_id, claim, now = _running_repo(tmp_path)
    with sessions.begin() as session:
        session.get(InferenceTaskRow, task_id).lease_expires_at = now - timedelta(
            seconds=1
        )

    replacement = repo.claim(task_id, org, "worker-b", now)

    assert replacement is not None
    assert replacement.fence_token == claim.fence_token + 1
    assert replacement.attempt_no == claim.attempt_no + 1
    with pytest.raises(StaleLease):
        repo.complete_no_defect(_command(claim, now))
    with sessions() as session:
        task = session.get(InferenceTaskRow, task_id)
        attempts = session.scalars(
            select(InferenceAttemptRow)
            .where(InferenceAttemptRow.task_id == task_id)
            .order_by(InferenceAttemptRow.attempt_no)
        ).all()
        state = session.get(CameraInferenceStateRow, (org, task.camera_id))
        assert task.status == TaskStatus.RUNNING.value
        assert task.fence_token == replacement.fence_token
        assert state.running_task_id == task_id
        assert [(attempt.attempt_no, attempt.outcome) for attempt in attempts] == [
            (claim.attempt_no, "LEASE_EXPIRED"),
            (replacement.attempt_no, None),
        ]
        assert attempts[0].finished_at is not None
        assert attempts[1].finished_at is None
    engine.dispose()


def test_expired_other_task_releases_camera_and_claims_ready_target(tmp_path):
    engine, sessions, repo, org, expired_task_id, expired_claim, now = _running_repo(
        tmp_path
    )
    target_task_id, target_artifact_id = uuid4(), uuid4()
    with sessions.begin() as session:
        expired_task = session.get(InferenceTaskRow, expired_task_id)
        expired_task.lease_expires_at = now - timedelta(seconds=1)
        session.add_all(
            [
                FrameArtifactRow(
                    artifact_id=target_artifact_id,
                    organization_id=org,
                    camera_id=expired_task.camera_id,
                    stream_session_id=uuid4(),
                    frame_sequence=2,
                    captured_at=now,
                    sha256="c" * 64,
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=target_task_id,
                    organization_id=org,
                    camera_id=expired_task.camera_id,
                    artifact_id=target_artifact_id,
                    idempotency_key="target",
                    status=TaskStatus.READY.value,
                    dispatch_seq=1,
                    attempt_count=0,
                    fence_token=0,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )

    target_claim = repo.claim(target_task_id, org, "worker-b", now)

    assert target_claim is not None
    with sessions() as session:
        expired_task = session.get(InferenceTaskRow, expired_task_id)
        expired_attempt = session.get(InferenceAttemptRow, expired_claim.attempt_id)
        target_task = session.get(InferenceTaskRow, target_task_id)
        state = session.get(CameraInferenceStateRow, (org, target_task.camera_id))
        assert expired_task.status == TaskStatus.RETRY_WAIT.value
        assert expired_task.next_attempt_at is not None
        assert expired_attempt.outcome == "LEASE_EXPIRED"
        assert expired_attempt.finished_at is not None
        assert target_task.status == TaskStatus.RUNNING.value
        assert state.running_task_id == target_task_id
    engine.dispose()


@pytest.mark.parametrize(
    ("attribute", "value"),
    (("fence_token", 999), ("worker_id", "impostor"), ("attempt_no", 999)),
)
def test_finalize_rejects_attempt_that_does_not_match_lease_identity(
    tmp_path, attribute, value
):
    engine, sessions, repo, org, task_id, claim, now = _running_repo(tmp_path)
    with sessions.begin() as session:
        setattr(session.get(InferenceAttemptRow, claim.attempt_id), attribute, value)

    with pytest.raises(StaleLease):
        repo.complete_no_defect(_command(claim, now))

    with sessions() as session:
        task = session.get(InferenceTaskRow, task_id)
        attempt = session.get(InferenceAttemptRow, claim.attempt_id)
        state = session.get(CameraInferenceStateRow, (org, task.camera_id))
        assert task.status == TaskStatus.RUNNING.value
        assert attempt.finished_at is None
        assert state.running_task_id == task_id
        assert session.scalars(select(PublishedInferenceResultRow)).all() == []
    engine.dispose()
