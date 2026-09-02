"""Real database coverage for durable recovery and compatibility control."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

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
from odp_api.modules.tasks.commands import (
    DeliveryOutcome,
    DeliveryRequest,
    QuarantineReason,
    UnscopedQuarantineCommand,
    WorkerDeliveryScope,
)
from odp_api.modules.tasks.models import FailureKind, TaskStatus
from odp_api.modules.tasks.recovery import SystemRecoveryScope
from odp_api.ports.tasks import AdmissionRejected


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
                    ready_count=1 if status is TaskStatus.READY else 0,
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
        schema_version=1,
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
    tenant, camera, task, now = _seed_task(repository)
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
        assert session.get(CameraInferenceStateRow, (tenant, camera)).ready_count == 1


def test_due_retry_stays_waiting_when_camera_ready_capacity_is_full(repository, system_scope):
    tenant, camera, task, now = _seed_task(repository)
    claim = repository.claim(task, tenant, "worker", now)
    assert claim is not None
    repository.record_failure(claim, FailureKind.RETRYABLE_INFRA, now)
    with repository._session_factory() as session:
        waiting = session.get(InferenceTaskRow, task)
        waiting.next_attempt_at = now - timedelta(seconds=1)
        for sequence in (2, 3):
            artifact_id, ready_task_id = uuid4(), uuid4()
            session.add_all(
                [
                    FrameArtifactRow(
                        artifact_id=artifact_id,
                        organization_id=tenant,
                        camera_id=camera,
                        stream_session_id=uuid4(),
                        frame_sequence=sequence,
                        captured_at=now,
                        object_key=f"frames/{sequence}.jpg",
                        sha256=f"{sequence:064x}",
                        content_length=1,
                        state="AVAILABLE",
                        lifecycle="PROCESSING",
                        created_at=now,
                        updated_at=now,
                    ),
                    InferenceTaskRow(
                        task_id=ready_task_id,
                        organization_id=tenant,
                        camera_id=camera,
                        artifact_id=artifact_id,
                        idempotency_key=str(ready_task_id),
                        status=TaskStatus.READY.value,
                        dispatch_seq=1,
                        attempt_count=0,
                        fence_token=0,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
        session.get(CameraInferenceStateRow, (tenant, camera)).ready_count = 2
        session.commit()

    assert repository.release_due_retries(now, system_scope) == 0
    with repository._session_factory() as session:
        assert session.get(InferenceTaskRow, task).status == TaskStatus.RETRY_WAIT.value
        assert session.get(CameraInferenceStateRow, (tenant, camera)).ready_count == 2
        assert session.query(OutboxEventRow).filter_by(task_id=task, dispatch_seq=2).count() == 0


def test_due_retry_scan_skips_capacity_blocked_prefix_before_applying_limit(
    repository, system_scope
):
    """Blocked early rows must not starve a later eligible camera forever."""
    now = datetime.now(UTC).replace(microsecond=0)
    blocked = [_seed_task(repository, status=TaskStatus.RETRY_WAIT) for _ in range(2)]
    eligible = _seed_task(repository, status=TaskStatus.RETRY_WAIT)
    with repository._session_factory() as session:
        for index, (tenant, camera, task, _) in enumerate(blocked, start=3):
            session.get(InferenceTaskRow, task).next_attempt_at = now - timedelta(
                seconds=index
            )
            for ready_sequence in (1, 2):
                artifact_id, ready_task_id = uuid4(), uuid4()
                session.add_all(
                    [
                        FrameArtifactRow(
                            artifact_id=artifact_id,
                            organization_id=tenant,
                            camera_id=camera,
                            stream_session_id=uuid4(),
                            frame_sequence=100 * index + ready_sequence,
                            captured_at=now,
                            object_key=f"blocked/{index}/{ready_sequence}.jpg",
                            sha256=f"{100 * index + ready_sequence:064x}",
                            content_length=1,
                            state="AVAILABLE",
                            lifecycle="PROCESSING",
                            created_at=now,
                            updated_at=now,
                        ),
                        InferenceTaskRow(
                            task_id=ready_task_id,
                            organization_id=tenant,
                            camera_id=camera,
                            artifact_id=artifact_id,
                            idempotency_key=str(ready_task_id),
                            status=TaskStatus.READY.value,
                            dispatch_seq=1,
                            attempt_count=0,
                            fence_token=0,
                            created_at=now,
                            updated_at=now,
                        ),
                    ]
                )
            session.get(CameraInferenceStateRow, (tenant, camera)).ready_count = 2
        session.get(InferenceTaskRow, eligible[2]).next_attempt_at = now - timedelta(
            seconds=1
        )
        session.commit()

    assert repository.release_due_retries(now, system_scope, limit=2) == 1
    assert [_row(repository, item[2]).status for item in blocked] == [
        TaskStatus.RETRY_WAIT.value,
        TaskStatus.RETRY_WAIT.value,
    ]
    released = _row(repository, eligible[2])
    assert (released.status, released.dispatch_seq) == (TaskStatus.READY.value, 2)


def test_due_retry_prefilter_uses_authoritative_ready_rows_not_cached_count(
    repository, system_scope
):
    """A stale materialized count must neither starve nor over-admit a camera."""
    now = datetime.now(UTC).replace(microsecond=0)
    blocked = _seed_task(repository, status=TaskStatus.RETRY_WAIT)
    eligible = _seed_task(repository, status=TaskStatus.RETRY_WAIT)
    with repository._session_factory() as session:
        session.get(InferenceTaskRow, blocked[2]).next_attempt_at = now - timedelta(
            seconds=2
        )
        session.get(InferenceTaskRow, eligible[2]).next_attempt_at = now - timedelta(
            seconds=1
        )
        for ready_sequence in (1, 2):
            artifact_id, ready_task_id = uuid4(), uuid4()
            session.add_all(
                [
                    FrameArtifactRow(
                        artifact_id=artifact_id,
                        organization_id=blocked[0],
                        camera_id=blocked[1],
                        stream_session_id=uuid4(),
                        frame_sequence=500 + ready_sequence,
                        captured_at=now,
                        object_key=f"stale-count/{ready_sequence}.jpg",
                        sha256=f"{500 + ready_sequence:064x}",
                        content_length=1,
                        state="AVAILABLE",
                        lifecycle="PROCESSING",
                        created_at=now,
                        updated_at=now,
                    ),
                    InferenceTaskRow(
                        task_id=ready_task_id,
                        organization_id=blocked[0],
                        camera_id=blocked[1],
                        artifact_id=artifact_id,
                        idempotency_key=str(ready_task_id),
                        status=TaskStatus.READY.value,
                        dispatch_seq=1,
                        attempt_count=0,
                        fence_token=0,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
        session.get(
            CameraInferenceStateRow, (blocked[0], blocked[1])
        ).ready_count = 0
        session.get(
            CameraInferenceStateRow, (eligible[0], eligible[1])
        ).ready_count = 2
        session.commit()

    assert repository.release_due_retries(now, system_scope, limit=2) == 1
    assert _row(repository, blocked[2]).status == TaskStatus.RETRY_WAIT.value
    assert _row(repository, eligible[2]).status == TaskStatus.READY.value


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


def test_stale_ready_with_any_lease_marker_is_not_redispatched(repository, system_scope):
    """A corrupt READY row must fail closed instead of racing its recorded owner."""
    tenant, _, task, _ = _seed_task(
        repository, last_dispatched_at=datetime.now(UTC) - timedelta(seconds=11)
    )
    with repository._session_factory() as session:
        row = session.get(InferenceTaskRow, task)
        row.lease_owner = "worker-still-recorded"
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=30)
        session.commit()

    assert repository.redispatch_stale_ready(
        datetime.now(UTC) + timedelta(days=3650), system_scope
    ) == 0
    row = _row(repository, task)
    assert (row.organization_id, row.dispatch_seq, row.lease_owner) == (
        tenant,
        1,
        "worker-still-recorded",
    )


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


def test_all_mutating_cross_tenant_recovery_sweeps_honor_explicit_batch_limits(
    repository, system_scope
):
    """Dropping a LIMIT from any existing scheduler mutation must exceed one row."""
    now = datetime.now(UTC).replace(microsecond=0)

    retry_tasks = [_seed_task(repository, status=TaskStatus.RETRY_WAIT)[2] for _ in range(2)]
    with repository._session_factory() as session:
        for task_id in retry_tasks:
            session.get(InferenceTaskRow, task_id).next_attempt_at = now - timedelta(seconds=1)
        session.commit()
    assert repository.release_due_retries(now, system_scope, limit=1) == 1
    assert sum(_row(repository, task).status == TaskStatus.RETRY_WAIT.value for task in retry_tasks) == 1

    stale_tasks = [
        _seed_task(
            repository,
            last_dispatched_at=now - timedelta(seconds=11),
        )[2]
        for _ in range(2)
    ]
    assert repository.redispatch_stale_ready(now, system_scope, limit=1) == 1
    assert sorted(_row(repository, task).dispatch_seq for task in stale_tasks) == [1, 2]

    expiring = [_seed_task(repository) for _ in range(2)]
    for tenant, _, task, _ in expiring:
        assert repository.claim(task, tenant, f"worker-{task}", now) is not None
    with repository._session_factory() as session:
        for _, _, task, _ in expiring:
            session.get(InferenceTaskRow, task).lease_expires_at = now - timedelta(seconds=1)
        session.commit()
    assert repository.expire_leases(now, system_scope, limit=1) == 1
    assert sum(_row(repository, task).status == TaskStatus.RUNNING.value for _, _, task, _ in expiring) == 1


def test_all_mutating_recovery_sweeps_reject_nonpositive_batch_limits(
    repository, system_scope
):
    now = datetime.now(UTC)
    operations = (
        repository.release_due_retries,
        repository.redispatch_stale_ready,
        repository.expire_leases,
        repository.release_expired_outbox_claims,
        repository.expire_stale_artifact_reservations,
    )
    for operation in operations:
        with pytest.raises(ValueError, match="positive"):
            operation(now, system_scope, limit=0)


def test_expired_outbox_claim_repair_is_bounded_and_only_clears_unpublished_claims(
    repository, system_scope
):
    """Published, live, or rows beyond the batch must not be changed."""
    seeded = [_seed_task(repository) for _ in range(4)]
    now = datetime.now(UTC).replace(microsecond=0)
    task_ids = [item[2] for item in seeded]
    with repository._session_factory() as session:
        rows = [
            session.scalar(select(OutboxEventRow).where(OutboxEventRow.task_id == task_id))
            for task_id in task_ids
        ]
        for row in rows:
            row.claim_owner = "relay-a"
            row.claim_expires_at = now - timedelta(seconds=1)
        rows[2].published_at = now - timedelta(seconds=2)
        rows[3].claim_expires_at = now + timedelta(seconds=10)
        session.commit()

    caller_clock_far_future = now + timedelta(days=3650)
    assert (
        repository.release_expired_outbox_claims(
            caller_clock_far_future, system_scope, limit=1
        )
        == 1
    )

    with repository._session_factory() as session:
        rows = [
            session.scalar(select(OutboxEventRow).where(OutboxEventRow.task_id == task_id))
            for task_id in task_ids
        ]
        released = [row for row in rows[:2] if row.claim_owner is None]
        assert len(released) == 1
        assert released[0].claim_expires_at is None
        assert (released[0].published_at, released[0].publish_attempts) == (None, 0)
        still_claimed = [row for row in rows[:2] if row.claim_owner is not None]
        assert len(still_claimed) == 1
        assert still_claimed[0].claim_expires_at is not None
        assert rows[2].claim_owner == "relay-a"
        assert rows[2].claim_expires_at is not None
        assert rows[2].published_at is not None
        assert rows[3].claim_owner == "relay-a"
        assert rows[3].claim_expires_at is not None


def test_recovery_counts_quarantine_without_replaying_or_deleting(repository, system_scope):
    """Quarantine is an observation metric, never a scheduler mutation."""
    tenant, _, task, now = _seed_task(repository)
    repository.quarantine_message(
        "odp:inference:tasks", "quarantine-count-1", uuid4(), "v9", "9", b"bad", task, tenant, now
    )
    with repository._session_factory() as session:
        before = session.scalar(select(MessageQuarantineRow))
        task_before = session.get(InferenceTaskRow, task)
        outbox_before = session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.task_id == task)
        )
        before_snapshot = (
            before.quarantine_id,
            before.status,
            before.replayed_at,
            before.raw_payload,
        )
        task_snapshot = (
            task_before.status,
            task_before.dispatch_seq,
            task_before.error_code,
        )
        outbox_snapshot = (
            outbox_before.outbox_id,
            outbox_before.published_at,
            outbox_before.claim_owner,
            outbox_before.publish_attempts,
        )

    assert repository.count_quarantined_messages(now, system_scope) == 1

    with repository._session_factory() as session:
        row = session.scalar(select(MessageQuarantineRow))
        task_after = session.get(InferenceTaskRow, task)
        outbox_after = session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.task_id == task)
        )
        assert row is not None
        assert (row.quarantine_id, row.status, row.replayed_at, row.raw_payload) == before_snapshot
        assert (
            task_after.status,
            task_after.dispatch_seq,
            task_after.error_code,
        ) == task_snapshot
        assert (
            outbox_after.outbox_id,
            outbox_after.published_at,
            outbox_after.claim_owner,
            outbox_after.publish_attempts,
        ) == outbox_snapshot


def test_stale_artifact_reservation_repair_is_camera_first_and_preserves_available(
    repository, system_scope
):
    """Only PENDING artifact state may be failed while expired anchors are cleared."""
    first = _seed_task(repository)
    second = _seed_task(repository)
    third = _seed_task(repository)
    live = _seed_task(repository)
    now = datetime.now(UTC).replace(microsecond=0)
    artifact_ids = []
    with repository._session_factory() as session:
        for tenant, camera, task, _ in (first, second, third, live):
            task_row = session.get(InferenceTaskRow, task)
            artifact = session.get(FrameArtifactRow, task_row.artifact_id)
            artifact_ids.append(artifact.artifact_id)
            state = session.get(CameraInferenceStateRow, (tenant, camera))
            state.reservation_id = artifact.artifact_id
            state.reservation_expires_at = now - timedelta(seconds=1)
        session.get(FrameArtifactRow, artifact_ids[0]).state = "PENDING"
        pending_evidence = session.get(FrameArtifactRow, artifact_ids[1])
        pending_evidence.state = "PENDING"
        pending_evidence.lifecycle = "EVIDENCE"
        session.get(FrameArtifactRow, artifact_ids[2]).lifecycle = "EVIDENCE"
        live_state = session.get(CameraInferenceStateRow, (live[0], live[1]))
        live_state.reservation_expires_at = now + timedelta(hours=1)
        session.commit()

    assert (
        repository.expire_stale_artifact_reservations(
            now + timedelta(days=3650), system_scope, limit=10
        )
        == 3
    )

    with repository._session_factory() as session:
        pending = session.get(FrameArtifactRow, artifact_ids[0])
        pending_evidence = session.get(FrameArtifactRow, artifact_ids[1])
        available = session.get(FrameArtifactRow, artifact_ids[2])
        assert (pending.state, pending.error_code) == (
            "FAILED",
            "ADMISSION_RESERVATION_EXPIRED",
        )
        assert (
            pending_evidence.state,
            pending_evidence.lifecycle,
            pending_evidence.error_code,
        ) == ("PENDING", "EVIDENCE", None)
        assert (available.state, available.lifecycle, available.error_code) == (
            "AVAILABLE",
            "EVIDENCE",
            None,
        )
        for tenant, camera, _, _ in (first, second, third):
            state = session.get(CameraInferenceStateRow, (tenant, camera))
            assert (state.reservation_id, state.reservation_expires_at) == (None, None)
        live_state = session.get(CameraInferenceStateRow, (live[0], live[1]))
        assert (
            live_state.reservation_id,
            _utc(live_state.reservation_expires_at),
        ) == (artifact_ids[3], now + timedelta(hours=1))


def test_artifact_reservation_repair_honors_batch_limit(repository, system_scope):
    """Removing the artifact LIMIT must clear both expired camera anchors."""
    seeded = (_seed_task(repository), _seed_task(repository))
    now = datetime.now(UTC).replace(microsecond=0)
    with repository._session_factory() as session:
        for index, (tenant, camera, task, _) in enumerate(seeded, start=1):
            artifact_id = session.get(InferenceTaskRow, task).artifact_id
            artifact = session.get(FrameArtifactRow, artifact_id)
            artifact.state = "PENDING"
            state = session.get(CameraInferenceStateRow, (tenant, camera))
            state.reservation_id = artifact_id
            state.reservation_expires_at = now - timedelta(seconds=3 - index)
        session.commit()

    assert repository.expire_stale_artifact_reservations(
        now, system_scope, limit=1
    ) == 1

    with repository._session_factory() as session:
        states = [
            session.get(CameraInferenceStateRow, (tenant, camera))
            for tenant, camera, _, _ in seeded
        ]
        assert sum(state.reservation_id is None for state in states) == 1
        assert sum(state.reservation_id is not None for state in states) == 1


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


def _delivery_request(
    tenant,
    task,
    *,
    message_id="delivery-1",
    dispatch_seq=1,
    quarantine_reason=None,
    event_type="vision.inference.requested.v1",
    schema_version="1",
    raw_payload=b"{}",
):
    return DeliveryRequest(
        stream_name="odp:inference:tasks",
        message_id=message_id,
        event_id=uuid4(),
        event_type=event_type,
        schema_version=schema_version,
        raw_payload=raw_payload,
        organization_id=tenant,
        task_id=task,
        expected_dispatch_seq=dispatch_seq,
        quarantine_reason=quarantine_reason,
    )


def test_worker_delivery_claim_requires_exact_dispatch_and_creates_one_attempt(repository):
    tenant, _, task, now = _seed_task(repository)
    decision = repository.accept_delivery(
        _delivery_request(tenant, task),
        "worker-a",
        now,
        WorkerDeliveryScope("worker-a"),
    )

    assert decision.outcome is DeliveryOutcome.CLAIMED
    assert decision.claim is not None and decision.claim.attempt_no == 1
    with repository._session_factory() as session:
        assert session.query(InferenceAttemptRow).filter_by(task_id=task).count() == 1


def test_future_dispatch_is_quarantined_without_mutating_current_ready_task(repository):
    tenant, _, task, now = _seed_task(repository)
    decision = repository.accept_delivery(
        _delivery_request(tenant, task, dispatch_seq=2, message_id="future-1"),
        "worker-a",
        now,
        WorkerDeliveryScope("worker-a"),
    )

    assert decision.outcome is DeliveryOutcome.QUARANTINED
    row = _row(repository, task)
    assert (row.status, row.dispatch_seq, row.attempt_count) == (
        TaskStatus.READY.value,
        1,
        0,
    )
    with repository._session_factory() as session:
        quarantine = session.scalar(
            select(MessageQuarantineRow).where(MessageQuarantineRow.message_id == "future-1")
        )
        assert (quarantine.error, quarantine.task_id, quarantine.organization_id) == (
            QuarantineReason.FUTURE_DISPATCH_SEQUENCE.value,
            None,
            tenant,
        )


def test_future_quarantine_is_sticky_after_dispatch_generation_advances(repository):
    tenant, _, task, now = _seed_task(repository)
    request = _delivery_request(tenant, task, dispatch_seq=2, message_id="future-sticky-1")
    first = repository.accept_delivery(
        request,
        "worker-a",
        now,
        WorkerDeliveryScope("worker-a"),
    )
    assert first.outcome is DeliveryOutcome.QUARANTINED

    with repository._session_factory() as session:
        session.get(InferenceTaskRow, task).dispatch_seq = 2
        session.commit()

    second = repository.accept_delivery(
        replace(request, raw_payload=b"rewritten", event_id=uuid4()),
        "worker-a",
        now,
        WorkerDeliveryScope("worker-a"),
    )

    assert second.outcome is DeliveryOutcome.QUARANTINED
    assert second.claim is None
    assert _row(repository, task).attempt_count == 0
    with repository._session_factory() as session:
        rows = session.query(MessageQuarantineRow).filter_by(message_id="future-sticky-1").all()
        assert len(rows) == 1
        assert (rows[0].error, rows[0].task_id, rows[0].raw_payload) == (
            QuarantineReason.FUTURE_DISPATCH_SEQUENCE.value,
            None,
            b"{}",
        )


def test_scoped_quarantine_persists_exact_reason_and_blocks_only_exact_dispatch(repository):
    tenant, _, task, now = _seed_task(repository)
    decision = repository.accept_delivery(
        _delivery_request(
            tenant,
            task,
            message_id="malformed-1",
            quarantine_reason=QuarantineReason.MALFORMED_ENVELOPE,
        ),
        "worker-a",
        now,
        WorkerDeliveryScope("worker-a"),
    )

    assert decision.outcome is DeliveryOutcome.QUARANTINED
    assert (_row(repository, task).status, _row(repository, task).error_code) == (
        TaskStatus.BLOCKED_COMPATIBILITY.value,
        QuarantineReason.MALFORMED_ENVELOPE.value,
    )
    with repository._session_factory() as session:
        quarantine = session.scalar(
            select(MessageQuarantineRow).where(MessageQuarantineRow.message_id == "malformed-1")
        )
        assert quarantine.error == QuarantineReason.MALFORMED_ENVELOPE.value


def test_unknown_task_is_durably_unscoped_and_ackable_without_foreign_key(repository):
    tenant, task, now = uuid4(), uuid4(), datetime.now(UTC)
    decision = repository.accept_delivery(
        _delivery_request(tenant, task, message_id="unknown-task-1"),
        "worker-a",
        now,
        WorkerDeliveryScope("worker-a"),
    )

    assert decision.outcome is DeliveryOutcome.QUARANTINED
    with repository._session_factory() as session:
        quarantine = session.scalar(
            select(MessageQuarantineRow).where(
                MessageQuarantineRow.message_id == "unknown-task-1"
            )
        )
        assert (quarantine.error, quarantine.task_id, quarantine.organization_id) == (
            QuarantineReason.UNRESOLVED_TASK_REFERENCE.value,
            None,
            tenant,
        )


def test_unknown_task_quarantine_is_sticky_if_task_appears_later(repository):
    tenant, task, now = uuid4(), uuid4(), datetime.now(UTC)
    request = _delivery_request(tenant, task, message_id="unknown-sticky-1")
    first = repository.accept_delivery(
        request,
        "worker-a",
        now,
        WorkerDeliveryScope("worker-a"),
    )
    assert first.outcome is DeliveryOutcome.QUARANTINED

    camera, artifact = uuid4(), uuid4()
    with repository._session_factory() as session:
        session.add_all(
            [
                CameraInferenceStateRow(
                    organization_id=tenant,
                    camera_id=camera,
                    running_task_id=None,
                    ready_count=1,
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
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=task,
                    organization_id=tenant,
                    camera_id=camera,
                    artifact_id=artifact,
                    idempotency_key=str(task),
                    status=TaskStatus.READY.value,
                    dispatch_seq=1,
                    attempt_count=0,
                    fence_token=0,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.commit()

    second = repository.accept_delivery(
        request,
        "worker-a",
        now,
        WorkerDeliveryScope("worker-a"),
    )

    assert second.outcome is DeliveryOutcome.QUARANTINED
    assert second.claim is None
    assert _row(repository, task).status == TaskStatus.READY.value
    assert _row(repository, task).attempt_count == 0
    with repository._session_factory() as session:
        rows = session.query(MessageQuarantineRow).filter_by(message_id="unknown-sticky-1").all()
        assert len(rows) == 1
        assert (rows[0].error, rows[0].task_id) == (
            QuarantineReason.UNRESOLVED_TASK_REFERENCE.value,
            None,
        )


def test_unscoped_quarantine_bounds_all_persisted_fields_and_requires_capability(repository):
    command = UnscopedQuarantineCommand(
        stream_name="s" * 400,
        message_id="m" * 400,
        event_id=uuid4(),
        event_type="e" * 400,
        schema_version="v" * 80,
        raw_payload=b"x" * 65536,
        reason=QuarantineReason.PAYLOAD_TOO_LARGE,
        organization_id=None,
    )

    with pytest.raises(TypeError):
        repository.quarantine_unscoped(command, datetime.now(UTC), object())
    result = repository.quarantine_unscoped(
        command,
        datetime.now(UTC),
        WorkerDeliveryScope("worker-a"),
    )

    assert result.ack_after_commit is True
    with repository._session_factory() as session:
        quarantine = session.get(MessageQuarantineRow, result.quarantine_id)
        assert (
            len(quarantine.stream_name),
            len(quarantine.message_id),
            len(quarantine.event_type),
            len(quarantine.schema_version),
            len(quarantine.raw_payload),
        ) == (255, 255, 255, 32, 65536)


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
    with pytest.raises(TypeError):
        repository.release_expired_outbox_claims(now, object())
    with pytest.raises(TypeError):
        repository.count_quarantined_messages(now, object())
    with pytest.raises(TypeError):
        repository.expire_stale_artifact_reservations(now, object())
    assert _row(repository, task).organization_id == tenant


def test_replay_only_blocked_creates_fresh_dispatch_and_durable_audit(repository):
    tenant, camera, task, now = _seed_task(repository)
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
        assert session.get(CameraInferenceStateRow, (tenant, camera)).ready_count == 1
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


def test_compatibility_replay_keeps_task_blocked_when_camera_capacity_is_full(
    repository,
):
    tenant, camera, task, now = _seed_task(repository)
    repository.quarantine_message(
        "inference.tasks", "capacity-0", uuid4(), "v9", "9", b"bad", task, tenant, now
    )
    with repository._session_factory() as session:
        assert session.get(CameraInferenceStateRow, (tenant, camera)).ready_count == 0
        for sequence in (2, 3):
            artifact_id, ready_task_id = uuid4(), uuid4()
            session.add_all(
                [
                    FrameArtifactRow(
                        artifact_id=artifact_id,
                        organization_id=tenant,
                        camera_id=camera,
                        stream_session_id=uuid4(),
                        frame_sequence=sequence,
                        captured_at=now,
                        object_key=f"frames/{sequence}.jpg",
                        sha256=f"{sequence:064x}",
                        content_length=1,
                        state="AVAILABLE",
                        lifecycle="PROCESSING",
                        created_at=now,
                        updated_at=now,
                    ),
                    InferenceTaskRow(
                        task_id=ready_task_id,
                        organization_id=tenant,
                        camera_id=camera,
                        artifact_id=artifact_id,
                        idempotency_key=str(ready_task_id),
                        status=TaskStatus.READY.value,
                        dispatch_seq=1,
                        attempt_count=0,
                        fence_token=0,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
        session.get(CameraInferenceStateRow, (tenant, camera)).ready_count = 2
        session.commit()

    with pytest.raises(AdmissionRejected, match="READY_WINDOW_FULL"):
        repository.replay_compatibility(task, tenant, now)

    with repository._session_factory() as session:
        assert session.get(InferenceTaskRow, task).status == TaskStatus.BLOCKED_COMPATIBILITY.value
        assert session.get(CameraInferenceStateRow, (tenant, camera)).ready_count == 2
        assert session.query(OutboxEventRow).filter_by(task_id=task, dispatch_seq=2).count() == 0
