"""Real database coverage for durable recovery and compatibility control."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from odp_api.adapters.persistence.models import AuditLogRow, Base
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
    MessageQuarantineRow,
    OutboxEventRow,
)
from odp_api.modules.tasks.models import FailureKind, TaskStatus
from odp_api.modules.tasks.recovery import SystemRecoveryScope
from odp_api.ports.tasks import AdmissionRejected
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(engine)
    yield SqlAlchemyTaskControlRepository(sessionmaker(bind=engine))
    engine.dispose()


def _seed_task(repository, *, attempt_count=0, last_dispatched_at=None, status=TaskStatus.READY):
    now = datetime.now(UTC).replace(microsecond=0)
    tenant, camera, artifact, task = uuid4(), uuid4(), uuid4(), uuid4()
    with repository._session_factory() as session:
        session.add_all(
            [
                CameraInferenceStateRow(
                    organization_id=tenant,
                    camera_id=camera,
                    running_task_id=None,
                    ready_count=1,
                    reservation_id=None,
                    reservation_expires_at=None,
                    last_admitted_at=None,
                    version=0,
                    updated_at=now,
                ),
                FrameArtifactRow(
                    artifact_id=artifact,
                    organization_id=tenant,
                    camera_id=camera,
                    stream_session_id=uuid4(),
                    frame_sequence=1,
                    captured_at=now,
                    object_key="x",
                    sha256="a" * 64,
                    content_length=1,
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    retention_until=None,
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=task,
                    organization_id=tenant,
                    camera_id=camera,
                    artifact_id=artifact,
                    idempotency_key=str(task),
                    status=status.value,
                    dispatch_seq=1,
                    attempt_count=attempt_count,
                    next_attempt_at=None,
                    last_dispatched_at=last_dispatched_at,
                    lease_owner=None,
                    fence_token=0,
                    lease_expires_at=None,
                    error_code=None,
                    error_detail=None,
                    created_at=now,
                    updated_at=now,
                ),
                _outbox(tenant, task, 1, now),
            ]
        )
        session.commit()
    return tenant, camera, task, now


def _outbox(tenant, task, dispatch_seq, now):
    return OutboxEventRow(
        outbox_id=uuid4(),
        organization_id=tenant,
        aggregate_type="inference_task",
        aggregate_id=task,
        task_id=task,
        dispatch_seq=dispatch_seq,
        event_type="vision.inference.requested.v1",
        schema_version="v1",
        payload={"task_id": str(task), "dispatch_seq": dispatch_seq},
        available_at=now,
        claim_owner=None,
        claim_expires_at=None,
        publish_attempts=0,
        published_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


def _row(repository, task):
    with repository._session_factory() as session:
        return session.get(InferenceTaskRow, task)


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@pytest.fixture
def system_scope():
    return SystemRecoveryScope("test-scheduler")


def test_attempt_one_retry_waits_one_second_then_releases_with_new_dispatch(
    repository, system_scope
):
    tenant, _, task, now = _seed_task(repository)
    claim = repository.claim(task, tenant, "worker", now)
    assert claim is not None
    repository.record_failure(claim, FailureKind.RETRYABLE_INFRA, now)
    assert (
        _row(repository, task).status,
        _utc(_row(repository, task).next_attempt_at) >= now + timedelta(seconds=1),
    ) == (TaskStatus.RETRY_WAIT.value, True)
    with repository._session_factory() as session:
        session.get(InferenceTaskRow, task).next_attempt_at = now - timedelta(seconds=1)
        session.commit()
    assert repository.release_due_retries(now + timedelta(seconds=1), system_scope) == 1
    row = _row(repository, task)
    assert (row.status, row.dispatch_seq, row.attempt_count) == (TaskStatus.READY.value, 2, 1)
    with repository._session_factory() as session:
        assert session.query(OutboxEventRow).filter_by(task_id=task, dispatch_seq=2).count() == 1


def test_second_retry_waits_two_seconds_and_third_failure_dead_letters(repository, system_scope):
    tenant, _, task, now = _seed_task(repository)
    first = repository.claim(task, tenant, "worker", now)
    assert first is not None
    repository.record_failure(first, FailureKind.RETRYABLE_INFRA, now)
    with repository._session_factory() as session:
        session.get(InferenceTaskRow, task).next_attempt_at = now - timedelta(seconds=1)
        session.commit()
    assert repository.release_due_retries(now + timedelta(seconds=1), system_scope) == 1
    second = repository.claim(task, tenant, "worker", now + timedelta(seconds=1))
    assert second is not None
    repository.record_failure(second, FailureKind.RETRYABLE_INFRA, now)
    assert _utc(_row(repository, task).next_attempt_at) >= now + timedelta(seconds=2)
    assert repository.release_due_retries(now + timedelta(seconds=1), system_scope) == 0
    with repository._session_factory() as session:
        session.get(InferenceTaskRow, task).next_attempt_at = now - timedelta(seconds=1)
        session.commit()
    assert repository.release_due_retries(now + timedelta(seconds=2), system_scope) == 1
    third = repository.claim(task, tenant, "worker", now + timedelta(seconds=2))
    assert third is not None
    repository.record_failure(third, FailureKind.RETRYABLE_INFRA, now)
    assert _row(repository, task).status == TaskStatus.DEAD_LETTER.value


@pytest.mark.parametrize("failure", [FailureKind.INVALID_INPUT, FailureKind.MODEL_CONFIGURATION])
def test_permanent_failures_dead_letter_immediately(repository, failure):
    tenant, _, task, now = _seed_task(repository)
    claim = repository.claim(task, tenant, "worker", now)
    assert claim is not None
    repository.record_failure(claim, failure, now)
    assert _row(repository, task).status == TaskStatus.DEAD_LETTER.value


def test_stale_ready_redispatches_once_but_fresh_ready_and_next_scan_do_not(
    repository, system_scope
):
    tenant, _, stale, _ = _seed_task(
        repository, last_dispatched_at=datetime.now(UTC) - timedelta(seconds=11)
    )
    _, _, fresh, now = _seed_task(repository, last_dispatched_at=datetime.now(UTC))
    assert repository.redispatch_stale_ready(now, system_scope) == 1
    assert (_row(repository, stale).dispatch_seq, _row(repository, fresh).dispatch_seq) == (2, 1)
    assert repository.redispatch_stale_ready(now, system_scope) == 0
    with repository._session_factory() as session:
        assert session.query(OutboxEventRow).filter_by(task_id=stale, dispatch_seq=2).count() == 1
    assert tenant != _row(repository, fresh).organization_id


def test_expired_running_task_finishes_attempt_and_clears_camera_anchor(repository, system_scope):
    tenant, camera, task, now = _seed_task(repository)
    claim = repository.claim(task, tenant, "worker", now)
    assert claim is not None
    with repository._session_factory() as session:
        session.get(InferenceTaskRow, task).lease_expires_at = now - timedelta(seconds=1)
        session.commit()
    assert repository.expire_leases(now, system_scope) == 1
    assert _row(repository, task).status == TaskStatus.RETRY_WAIT.value
    with repository._session_factory() as session:
        attempt = session.scalar(
            select(InferenceAttemptRow).where(InferenceAttemptRow.task_id == task)
        )
        anchor = session.scalar(
            select(CameraInferenceStateRow).where(
                CameraInferenceStateRow.organization_id == tenant,
                CameraInferenceStateRow.camera_id == camera,
            )
        )
        assert (attempt.outcome, attempt.finished_at is not None, anchor.running_task_id) == (
            "LEASE_EXPIRED",
            True,
            None,
        )


def test_expired_final_attempt_dead_letters_at_retry_cap(repository, system_scope):
    tenant, _, task, now = _seed_task(repository, attempt_count=2)
    claim = repository.claim(task, tenant, "worker", now)
    assert claim is not None and claim.attempt_no == 3
    with repository._session_factory() as session:
        session.get(InferenceTaskRow, task).lease_expires_at = now - timedelta(seconds=1)
        session.commit()
    assert repository.expire_leases(now, system_scope) == 1
    assert _row(repository, task).status == TaskStatus.DEAD_LETTER.value


def test_quarantine_caps_payload_blocks_tenant_task_and_only_acks_after_commit(repository):
    tenant, _, task, now = _seed_task(repository)
    result = repository.quarantine_message(
        "inference.tasks",
        "17-0",
        uuid4(),
        "vision.inference.requested.v9",
        "9",
        b"x" * 70000,
        task,
        tenant,
        now,
    )
    assert result.ack_after_commit is True
    assert _row(repository, task).status == TaskStatus.BLOCKED_COMPATIBILITY.value
    with repository._session_factory() as session:
        assert (
            len(
                session.scalar(
                    select(MessageQuarantineRow).where(MessageQuarantineRow.task_id == task)
                ).raw_payload
            )
            == 65536
        )


def test_quarantine_wrong_tenant_rolls_back_without_record(repository):
    tenant, _, task, now = _seed_task(repository)
    with pytest.raises(AdmissionRejected):
        repository.quarantine_message(
            "inference.tasks", "18-0", uuid4(), "v9", "9", b"bad", task, uuid4(), now
        )
    with repository._session_factory() as session:
        assert session.query(MessageQuarantineRow).count() == 0
    assert (_row(repository, task).organization_id, _row(repository, task).status) == (
        tenant,
        TaskStatus.READY.value,
    )


@pytest.mark.parametrize(
    "status",
    [TaskStatus.RUNNING, TaskStatus.RETRY_WAIT, TaskStatus.SUCCEEDED, TaskStatus.DEAD_LETTER],
)
def test_quarantine_rejects_non_ready_task_without_persisting_a_row(repository, status):
    tenant, _, task, now = _seed_task(repository, status=status)
    with pytest.raises(AdmissionRejected):
        repository.quarantine_message(
            "inference.tasks", "blocked-0", uuid4(), "v9", "9", b"bad", task, tenant, now
        )
    with repository._session_factory() as session:
        assert session.query(MessageQuarantineRow).count() == 0
    assert _row(repository, task).status == status.value


def test_quarantine_duplicate_message_for_blocked_task_is_idempotently_ackable(repository):
    tenant, _, task, now = _seed_task(repository)
    original = repository.quarantine_message(
        "inference.tasks", "same-0", uuid4(), "v9", "9", b"bad", task, tenant, now
    )
    duplicate = repository.quarantine_message(
        "inference.tasks", "same-0", uuid4(), "v9", "9", b"changed", task, tenant, now
    )
    assert (duplicate.quarantine_id, duplicate.ack_after_commit) == (original.quarantine_id, True)
    with repository._session_factory() as session:
        assert session.query(MessageQuarantineRow).count() == 1


@pytest.mark.parametrize(
    "status", [TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.SUCCEEDED, TaskStatus.DEAD_LETTER]
)
def test_quarantine_duplicate_rejects_task_that_is_no_longer_compatibility_blocked(
    repository, status
):
    tenant, _, task, now = _seed_task(repository)
    repository.quarantine_message(
        "inference.tasks", "stale-duplicate-0", uuid4(), "v9", "9", b"original", task, tenant, now
    )
    with repository._session_factory() as session:
        row = session.get(InferenceTaskRow, task)
        row.status = status.value
        session.commit()

    with pytest.raises(AdmissionRejected):
        repository.quarantine_message(
            "inference.tasks",
            "stale-duplicate-0",
            uuid4(),
            "v9",
            "9",
            b"duplicate",
            task,
            tenant,
            now,
        )

    with repository._session_factory() as session:
        assert session.query(MessageQuarantineRow).count() == 1
    assert _row(repository, task).status == status.value


def test_recovery_repository_rejects_missing_or_invalid_system_scope(repository):
    tenant, _, task, now = _seed_task(repository)
    with pytest.raises(TypeError):
        repository.release_due_retries(now)
    with pytest.raises(TypeError):
        repository.redispatch_stale_ready(now, object())
    with pytest.raises(TypeError):
        repository.expire_leases(now, object())
    assert _row(repository, task).organization_id == tenant


def test_replay_only_blocked_creates_fresh_dispatch_and_durable_audit(repository):
    tenant, _, task, now = _seed_task(repository)
    repository.quarantine_message(
        "inference.tasks", "19-0", uuid4(), "v9", "9", b"bad", task, tenant, now
    )
    result = repository.replay_compatibility(task, tenant, now)
    assert (result.replayed, result.dispatch_seq, result.new_outbox_id is not None) == (
        True,
        2,
        True,
    )
    assert _row(repository, task).status == TaskStatus.READY.value
    with repository._session_factory() as session:
        assert session.query(OutboxEventRow).filter_by(task_id=task, dispatch_seq=2).count() == 1
        assert (
            session.query(AuditLogRow)
            .filter_by(organization_id=tenant, resource_id=task, action="COMPATIBILITY_REPLAYED")
            .count()
            == 1
        )
    with pytest.raises(AdmissionRejected):
        repository.replay_compatibility(task, tenant, now)
    with pytest.raises(AdmissionRejected):
        repository.replay_compatibility(task, uuid4(), now)
