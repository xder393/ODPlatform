"""Real PostgreSQL session advisory-lock coverage for the recovery scheduler."""

import asyncio
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from alembic import command
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.tasks.models import TaskStatus
from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope

pytestmark = pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL CI database",
)


@pytest.fixture
def postgres_recovery_url():
    """Create one migrated database per test and remove it after all pools close."""
    shared_url = make_url(os.environ["ODP_POSTGRES_TEST_URL"])
    database_name = f"odp_task3_recovery_{uuid4().hex}"
    if re.fullmatch(r"[a-z0-9_]+", database_name) is None:
        raise AssertionError("generated PostgreSQL test database name is unsafe")
    admin_engine = create_engine(
        shared_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    isolated_url = shared_url.set(database=database_name).render_as_string(
        hide_password=False
    )
    # Mark cleanup authority before CREATE: a PostgreSQL client can lose the
    # response after the server has already created the database.
    created = True
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", isolated_url.replace("%", "%%"))
        command.upgrade(config, "head")
        yield isolated_url
    finally:
        try:
            if created:
                with admin_engine.connect() as connection:
                    connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                        ),
                        {"database_name": database_name},
                    )
                    connection.exec_driver_sql(
                        f'DROP DATABASE IF EXISTS "{database_name}"'
                    )
        finally:
            admin_engine.dispose()


def _recording_repository(calls, *, error=None):
    def operation(name):
        def run(now, scope):
            calls.append(name)
            if error is not None and name == "leases":
                raise error
            return 1

        return run

    return SimpleNamespace(
        expire_leases=operation("leases"),
        release_due_retries=operation("retries"),
        redispatch_stale_ready=operation("ready"),
        release_expired_outbox_claims=operation("outbox"),
        count_quarantined_messages=operation("quarantine"),
        expire_stale_artifact_reservations=operation("artifact"),
    )


def test_two_postgresql_schedulers_exclude_loser_without_mutation(
    postgres_recovery_url,
):
    """A session lock held elsewhere must make the complete second sweep a no-op."""
    from odp_api.processes.recovery_scheduler import (
        RECOVERY_ADVISORY_LOCK_KEY,
        PostgreSqlAdvisoryLock,
        RecoveryScheduler,
    )

    holder_engine, _ = create_engine_and_session(postgres_recovery_url)
    contender_engine, _ = create_engine_and_session(postgres_recovery_url)
    held_lock = PostgreSqlAdvisoryLock(holder_engine, key=RECOVERY_ADVISORY_LOCK_KEY)
    contender_lock = PostgreSqlAdvisoryLock(
        contender_engine, key=RECOVERY_ADVISORY_LOCK_KEY
    )
    lease = held_lock.try_acquire()
    assert lease is not None
    with contender_engine.connect() as connection:
        contender_backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
    assert lease.backend_pid != contender_backend_pid
    calls = []
    scheduler = RecoveryScheduler(
        RecoveryService(
            _recording_repository(calls), SystemRecoveryScope("postgres-loser")
        ),
        contender_lock,
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )
    try:
        result = scheduler.run_once()
        assert result.skipped_locked is True
        assert calls == []
    finally:
        lease.release()
        contender_engine.dispose()
        holder_engine.dispose()


def test_postgresql_scheduler_error_releases_session_lock_for_next_scheduler(
    postgres_recovery_url,
):
    """An exception in any transaction must not strand the session-level lock."""
    from odp_api.processes.recovery_scheduler import (
        RECOVERY_ADVISORY_LOCK_KEY,
        PostgreSqlAdvisoryLock,
        RecoveryScheduler,
    )

    first_engine, _ = create_engine_and_session(postgres_recovery_url)
    verifier_engine, _ = create_engine_and_session(postgres_recovery_url)
    first = RecoveryScheduler(
        RecoveryService(
            _recording_repository([], error=RuntimeError("injected sweep failure")),
            SystemRecoveryScope("postgres-error"),
        ),
        PostgreSqlAdvisoryLock(first_engine, key=RECOVERY_ADVISORY_LOCK_KEY),
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )
    try:
        with pytest.raises(RuntimeError, match="injected sweep failure"):
            first.run_once()
        with first_engine.connect() as connection:
            first_backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
        lease = PostgreSqlAdvisoryLock(
            verifier_engine, key=RECOVERY_ADVISORY_LOCK_KEY
        ).try_acquire()
        assert lease is not None
        assert lease.backend_pid != first_backend_pid
        lease.release()
    finally:
        verifier_engine.dispose()
        first_engine.dispose()


def test_postgresql_lock_blocks_real_outbox_repair_then_winner_clears_claim(
    postgres_recovery_url,
):
    """Leadership must guard persisted mutations, not only a fake call list."""
    from odp_api.processes.recovery_scheduler import (
        RECOVERY_ADVISORY_LOCK_KEY,
        PostgreSqlAdvisoryLock,
        RecoveryScheduler,
    )

    holder_engine, _ = create_engine_and_session(postgres_recovery_url)
    scheduler_engine, scheduler_sessions = create_engine_and_session(
        postgres_recovery_url
    )
    now = datetime.now(UTC).replace(microsecond=0)
    outbox_id = uuid4()
    with scheduler_sessions.begin() as session:
        session.add(
            OutboxEventRow(
                outbox_id=outbox_id,
                organization_id=uuid4(),
                aggregate_type="scheduler_lock_test",
                aggregate_id=uuid4(),
                task_id=None,
                dispatch_seq=None,
                event_type="test.scheduler.lock.v1",
                schema_version=1,
                payload={},
                available_at=now,
                claim_owner="stuck-relay",
                claim_expires_at=now - datetime.resolution,
                publish_attempts=0,
                published_at=None,
                created_at=now,
                updated_at=now,
            )
        )

    holder = PostgreSqlAdvisoryLock(holder_engine, key=RECOVERY_ADVISORY_LOCK_KEY)
    contender = PostgreSqlAdvisoryLock(
        scheduler_engine, key=RECOVERY_ADVISORY_LOCK_KEY
    )
    lease = holder.try_acquire()
    assert lease is not None
    scheduler = RecoveryScheduler(
        RecoveryService(
            SqlAlchemyTaskControlRepository(scheduler_sessions),
            SystemRecoveryScope("postgres-real-mutation"),
        ),
        contender,
        clock=lambda: now + datetime.resolution,
    )
    try:
        with scheduler_engine.connect() as connection:
            contender_backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
        assert lease.backend_pid != contender_backend_pid

        loser = scheduler.run_once()
        assert loser.skipped_locked is True
        with scheduler_sessions() as session:
            row = session.get(OutboxEventRow, outbox_id)
            assert (row.claim_owner, row.claim_expires_at is not None) == (
                "stuck-relay",
                True,
            )

        lease.release()
        winner = scheduler.run_once()
        assert winner.skipped_locked is False
        assert winner.recovery is not None
        assert winner.recovery.outbox_claims_released >= 1
        with scheduler_sessions() as session:
            row = session.get(OutboxEventRow, outbox_id)
            assert (row.claim_owner, row.claim_expires_at) == (None, None)
    finally:
        lease.release()
        with scheduler_sessions.begin() as session:
            row = session.scalar(
                select(OutboxEventRow).where(OutboxEventRow.outbox_id == outbox_id)
            )
            if row is not None:
                session.delete(row)
        scheduler_engine.dispose()
        holder_engine.dispose()


def test_postgresql_expired_lease_ignores_mismatched_camera_anchor(
    postgres_recovery_url,
):
    """A duplicate wrong-camera anchor must not mutate another camera's Task."""
    engine, sessions = create_engine_and_session(postgres_recovery_url)
    now = datetime.now(UTC).replace(microsecond=0)
    organization_id = uuid4()
    correct_camera_id = uuid4()
    wrong_camera_id = uuid4()
    session_id = uuid4()
    artifact_id = uuid4()
    task_id = uuid4()
    with sessions.begin() as session:
        session.add(
            InspectionSessionRow(
                session_id=session_id,
                organization_id=organization_id,
                camera_id=correct_camera_id,
                line_id=uuid4(),
                source_type="TEST",
                sanitized_uri="rtsp://test.invalid/recovery-anchor",
                status="RUNNING",
                idempotency_key=str(uuid4()),
                started_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            FrameArtifactRow(
                artifact_id=artifact_id,
                organization_id=organization_id,
                camera_id=correct_camera_id,
                stream_session_id=session_id,
                frame_sequence=1,
                captured_at=now,
                object_key="recovery-anchor/frame.jpg",
                sha256="a" * 64,
                content_length=1,
                state="AVAILABLE",
                lifecycle="PROCESSING",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            InferenceTaskRow(
                task_id=task_id,
                organization_id=organization_id,
                camera_id=correct_camera_id,
                artifact_id=artifact_id,
                idempotency_key=str(uuid4()),
                status=TaskStatus.RUNNING.value,
                dispatch_seq=1,
                attempt_count=1,
                lease_owner="worker-a",
                fence_token=1,
                lease_expires_at=now - timedelta(seconds=1),
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add_all(
            [
                CameraInferenceStateRow(
                    organization_id=organization_id,
                    camera_id=correct_camera_id,
                    running_task_id=task_id,
                    ready_count=0,
                    version=0,
                    updated_at=now,
                ),
                CameraInferenceStateRow(
                    organization_id=organization_id,
                    camera_id=wrong_camera_id,
                    running_task_id=task_id,
                    ready_count=0,
                    version=0,
                    updated_at=now,
                ),
            ]
        )

    blocker = sessions()
    blocker.begin()
    blocker.scalar(
        select(CameraInferenceStateRow)
        .where(
            CameraInferenceStateRow.organization_id == organization_id,
            CameraInferenceStateRow.camera_id == correct_camera_id,
        )
        .with_for_update()
    )
    repository = SqlAlchemyTaskControlRepository(sessions)
    scope = SystemRecoveryScope("wrong-camera-anchor")
    try:
        assert repository.expire_leases(now, scope) == 0
        with sessions() as session:
            task = session.get(InferenceTaskRow, task_id)
            wrong = session.get(
                CameraInferenceStateRow, (organization_id, wrong_camera_id)
            )
            assert task.status == TaskStatus.RUNNING.value
            assert wrong.running_task_id == task_id

        blocker.rollback()
        assert repository.expire_leases(now, scope) == 1
        with sessions() as session:
            task = session.get(InferenceTaskRow, task_id)
            correct = session.get(
                CameraInferenceStateRow, (organization_id, correct_camera_id)
            )
            wrong = session.get(
                CameraInferenceStateRow, (organization_id, wrong_camera_id)
            )
            assert task.status == TaskStatus.RETRY_WAIT.value
            assert correct.running_task_id is None
            assert wrong.running_task_id == task_id
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()


@pytest.mark.anyio
async def test_postgresql_inflight_scheduler_lock_survives_repeated_cancellation(
    postgres_recovery_url,
):
    """A blocked sweep excludes a peer until its worker releases the real lock."""
    from odp_api.processes.recovery_scheduler import (
        RECOVERY_ADVISORY_LOCK_KEY,
        PostgreSqlAdvisoryLock,
        RecoveryScheduler,
    )

    first_engine, _ = create_engine_and_session(postgres_recovery_url)
    second_engine, _ = create_engine_and_session(postgres_recovery_url)
    third_engine, _ = create_engine_and_session(postgres_recovery_url)
    entered = threading.Event()
    permit_finish = threading.Event()
    loser_calls = []

    def blocking_operation(now, scope):
        entered.set()
        assert permit_finish.wait(timeout=3)
        return 0

    first_repo = SimpleNamespace(
        release_due_retries=blocking_operation,
        redispatch_stale_ready=lambda now, scope: 0,
        expire_leases=lambda now, scope: 0,
        release_expired_outbox_claims=lambda now, scope: 0,
        count_quarantined_messages=lambda now, scope: 0,
        expire_stale_artifact_reservations=lambda now, scope: 0,
    )
    first = RecoveryScheduler(
        RecoveryService(first_repo, SystemRecoveryScope("postgres-inflight-first")),
        PostgreSqlAdvisoryLock(first_engine, key=RECOVERY_ADVISORY_LOCK_KEY),
        clock=lambda: datetime.now(UTC),
    )
    second = RecoveryScheduler(
        RecoveryService(
            _recording_repository(loser_calls),
            SystemRecoveryScope("postgres-inflight-loser"),
        ),
        PostgreSqlAdvisoryLock(second_engine, key=RECOVERY_ADVISORY_LOCK_KEY),
        clock=lambda: datetime.now(UTC),
    )
    task = asyncio.create_task(first.run_once_async())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        loser = await asyncio.to_thread(second.run_once)
        assert loser.skipped_locked is True
        assert loser_calls == []

        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False

        permit_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        third = PostgreSqlAdvisoryLock(
            third_engine, key=RECOVERY_ADVISORY_LOCK_KEY
        ).try_acquire()
        assert third is not None
        third.release()
    finally:
        permit_finish.set()
        if not task.done():
            with pytest.raises(asyncio.CancelledError):
                await task
        third_engine.dispose()
        second_engine.dispose()
        first_engine.dispose()


def test_postgresql_recovery_tests_use_a_fresh_migrated_database(
    postgres_recovery_url,
):
    """The recovery module must never create unversioned tables in the shared URL."""
    assert postgres_recovery_url != os.environ["ODP_POSTGRES_TEST_URL"]
    engine, _ = create_engine_and_session(postgres_recovery_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()


def test_postgresql_due_retry_plan_uses_status_scoped_recovery_indexes(
    postgres_recovery_url,
):
    """The release scan must use both partial indexes, not a full COUNT scan."""
    engine, _ = create_engine_and_session(postgres_recovery_url)
    try:
        with engine.begin() as connection:
            # Keep the plan regression representative of the review's 100k-row
            # measurement while using one immutable Artifact parent.  Two READY
            # rows make the correlated OFFSET 1 capacity probe meaningful.
            connection.exec_driver_sql(
                """
                INSERT INTO inspection_sessions (
                    session_id, organization_id, camera_id, line_id,
                    source_type, sanitized_uri, status, idempotency_key,
                    started_at, created_at, updated_at
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001',
                    '00000000-0000-0000-0000-000000000002',
                    '00000000-0000-0000-0000-000000000003',
                    '00000000-0000-0000-0000-000000000004',
                    'TEST', 'rtsp://test.invalid/plan', 'RUNNING',
                    'plan-session', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO frame_artifacts (
                    artifact_id, organization_id, camera_id, stream_session_id,
                    frame_sequence, captured_at, object_key, sha256,
                    content_length, state, lifecycle, created_at, updated_at
                ) VALUES (
                    '00000000-0000-0000-0000-000000000005',
                    '00000000-0000-0000-0000-000000000002',
                    '00000000-0000-0000-0000-000000000003',
                    '00000000-0000-0000-0000-000000000001',
                    1, CURRENT_TIMESTAMP, 'plan/frame.jpg',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    1, 'AVAILABLE', 'PROCESSING', CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO inference_tasks (
                    task_id, organization_id, camera_id, artifact_id,
                    idempotency_key, status, dispatch_seq, attempt_count,
                    next_attempt_at, last_dispatched_at, lease_owner,
                    fence_token, lease_expires_at, created_at, updated_at
                )
                SELECT
                    md5('plan-task-' || series)::uuid,
                    '00000000-0000-0000-0000-000000000002',
                    '00000000-0000-0000-0000-000000000003',
                    '00000000-0000-0000-0000-000000000005',
                    'plan-task-' || series,
                    CASE WHEN series < 2 THEN 'READY' ELSE 'RETRY_WAIT' END,
                    1, 0,
                    CASE
                        WHEN series < 2 THEN NULL
                        ELSE CURRENT_TIMESTAMP - INTERVAL '1 minute'
                    END,
                    NULL, NULL, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM generate_series(0, 99999) AS generated(series)
                """
            )
            connection.exec_driver_sql("ANALYZE inference_tasks")

        query = """
            SELECT task_id
            FROM inference_tasks AS candidate
            WHERE candidate.status = 'RETRY_WAIT'
              AND candidate.next_attempt_at <= CURRENT_TIMESTAMP
              AND NOT EXISTS (
                  SELECT ready.task_id
                  FROM inference_tasks AS ready
                  WHERE ready.organization_id = candidate.organization_id
                    AND ready.camera_id = candidate.camera_id
                    AND ready.status = 'READY'
                  LIMIT 1
                  OFFSET 1
              )
            ORDER BY candidate.next_attempt_at, candidate.task_id
            LIMIT 100
        """
        with engine.begin() as connection:
            # Plan shape is the invariant; do not turn this into a timing test.
            plan = connection.exec_driver_sql(
                f"EXPLAIN (FORMAT JSON, COSTS OFF) {query}"
            ).scalar_one()
        rendered = str(plan)
        assert "ix_inference_tasks_retry_wait_next_attempt_task" in rendered
        assert "ix_inference_tasks_ready_organization_camera" in rendered
        assert "Seq Scan" not in rendered
    finally:
        engine.dispose()
