"""Real PostgreSQL session advisory-lock coverage for the recovery scheduler."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import OutboxEventRow
from odp_api.db import create_engine_and_session
from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope

pytestmark = pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL CI database",
)


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


def test_two_postgresql_schedulers_exclude_loser_without_mutation():
    """A session lock held elsewhere must make the complete second sweep a no-op."""
    from odp_api.processes.recovery_scheduler import (
        RECOVERY_ADVISORY_LOCK_KEY,
        PostgreSqlAdvisoryLock,
        RecoveryScheduler,
    )

    holder_engine, _ = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    contender_engine, _ = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
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


def test_postgresql_scheduler_error_releases_session_lock_for_next_scheduler():
    """An exception in any transaction must not strand the session-level lock."""
    from odp_api.processes.recovery_scheduler import (
        RECOVERY_ADVISORY_LOCK_KEY,
        PostgreSqlAdvisoryLock,
        RecoveryScheduler,
    )

    first_engine, _ = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    verifier_engine, _ = create_engine_and_session(
        os.environ["ODP_POSTGRES_TEST_URL"]
    )
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


def test_postgresql_lock_blocks_real_outbox_repair_then_winner_clears_claim():
    """Leadership must guard persisted mutations, not only a fake call list."""
    from odp_api.processes.recovery_scheduler import (
        RECOVERY_ADVISORY_LOCK_KEY,
        PostgreSqlAdvisoryLock,
        RecoveryScheduler,
    )

    holder_engine, _ = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    scheduler_engine, scheduler_sessions = create_engine_and_session(
        os.environ["ODP_POSTGRES_TEST_URL"]
    )
    Base.metadata.create_all(scheduler_engine)
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
