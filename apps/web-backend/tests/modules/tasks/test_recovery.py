"""Unit coverage for the scheduler orchestration boundary."""

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


def test_recovery_service_reports_each_scheduler_transition():
    """Removing any recovery operation must make its summary count wrong."""
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope

    repo = SimpleNamespace(
        release_due_retries=lambda now, scope: 2,
        redispatch_stale_ready=lambda now, scope: 3,
        expire_leases=lambda now, scope: 1,
        release_expired_outbox_claims=lambda now, scope: 4,
        count_quarantined_messages=lambda now, scope: 5,
        expire_stale_artifact_reservations=lambda now, scope: 6,
    )
    summary = RecoveryService(repo, SystemRecoveryScope("scheduler")).run_once(datetime.now(UTC))
    assert (
        summary.retries_released,
        summary.stale_ready_redispatched,
        summary.leases_expired,
        summary.outbox_claims_released,
        summary.quarantined_messages,
        summary.artifact_reservations_expired,
    ) == (
        2,
        3,
        1,
        4,
        5,
        6,
    )


def test_recovery_service_requires_a_typed_system_scope():
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope

    repo = SimpleNamespace(
        release_due_retries=lambda now, scope: 0,
        redispatch_stale_ready=lambda now, scope: 0,
        expire_leases=lambda now, scope: 0,
        release_expired_outbox_claims=lambda now, scope: 0,
        count_quarantined_messages=lambda now, scope: 0,
        expire_stale_artifact_reservations=lambda now, scope: 0,
    )
    with pytest.raises(TypeError):
        RecoveryService(repo, object())
    assert (
        RecoveryService(repo, SystemRecoveryScope("scheduler"))
        .run_once(datetime.now(UTC))
        .retries_released
        == 0
    )


def test_recovery_scheduler_loser_skips_the_entire_database_sweep():
    """A failed nonblocking lock acquisition must make zero repository calls."""
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope
    from odp_api.processes.recovery_scheduler import RecoveryScheduler

    calls = []

    def mutate(name):
        def operation(now, scope):
            calls.append(name)
            return 1

        return operation

    repo = SimpleNamespace(
        release_due_retries=mutate("retry"),
        redispatch_stale_ready=mutate("ready"),
        expire_leases=mutate("lease"),
        release_expired_outbox_claims=mutate("outbox"),
        count_quarantined_messages=mutate("quarantine"),
        expire_stale_artifact_reservations=mutate("artifact"),
    )
    lock = SimpleNamespace(try_acquire=lambda: None)
    scheduler = RecoveryScheduler(
        RecoveryService(repo, SystemRecoveryScope("scheduler-loser")),
        lock,
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )

    result = scheduler.run_once()

    assert result.skipped_locked is True
    assert result.recovery is None
    assert calls == []


def test_recovery_scheduler_releases_session_lock_when_sweep_raises():
    """Removing the finally-release path must leave the fake lease unreleased."""
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope
    from odp_api.processes.recovery_scheduler import RecoveryScheduler

    lease = SimpleNamespace(released=False)

    def release():
        lease.released = True

    lease.release = release
    repo = SimpleNamespace(
        expire_leases=lambda now, scope: (_ for _ in ()).throw(RuntimeError("boom")),
        release_due_retries=lambda now, scope: 0,
        redispatch_stale_ready=lambda now, scope: 0,
        release_expired_outbox_claims=lambda now, scope: 0,
        count_quarantined_messages=lambda now, scope: 0,
        expire_stale_artifact_reservations=lambda now, scope: 0,
    )
    scheduler = RecoveryScheduler(
        RecoveryService(repo, SystemRecoveryScope("scheduler-error")),
        SimpleNamespace(try_acquire=lambda: lease),
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )

    with pytest.raises(RuntimeError, match="boom"):
        scheduler.run_once()

    assert lease.released is True


@pytest.mark.anyio
async def test_recovery_loop_offloads_sync_database_sweep_and_waits_two_seconds(monkeypatch):
    """Running repository calls on the event-loop thread or changing cadence must fail."""
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope
    from odp_api.processes import recovery_scheduler as scheduler_module

    main_thread = threading.get_ident()
    sweep_threads = []
    released = []
    observed_timeouts = []

    def operation(now, scope):
        sweep_threads.append(threading.get_ident())
        return 0

    repo = SimpleNamespace(
        expire_leases=operation,
        release_due_retries=operation,
        redispatch_stale_ready=operation,
        release_expired_outbox_claims=operation,
        count_quarantined_messages=operation,
        expire_stale_artifact_reservations=operation,
    )

    def try_acquire():
        return SimpleNamespace(release=lambda: released.append(True))

    stop = asyncio.Event()

    async def observe_wait(awaitable, *, timeout):
        observed_timeouts.append(timeout)
        awaitable.close()
        stop.set()

    monkeypatch.setattr(scheduler_module.asyncio, "wait_for", observe_wait)
    scheduler = scheduler_module.RecoveryScheduler(
        RecoveryService(repo, SystemRecoveryScope("async-loop")),
        SimpleNamespace(try_acquire=try_acquire),
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )

    await scheduler.run(stop)

    assert len(sweep_threads) == 6
    assert all(thread_id != main_thread for thread_id in sweep_threads)
    assert observed_timeouts == [2.0]
    assert released == [True]
    assert scheduler.last_summary is not None


@pytest.mark.anyio
async def test_recovery_loop_isolates_one_failed_iteration_and_continues(monkeypatch):
    """One database outage must not terminate the long-running scheduler."""
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope
    from odp_api.processes import recovery_scheduler as scheduler_module

    attempts = []

    def expire(now, scope):
        attempts.append("expire")
        if len(attempts) == 1:
            raise OSError("database unavailable")
        return 0

    repo = SimpleNamespace(
        expire_leases=expire,
        release_due_retries=lambda now, scope: 0,
        redispatch_stale_ready=lambda now, scope: 0,
        release_expired_outbox_claims=lambda now, scope: 0,
        count_quarantined_messages=lambda now, scope: 0,
        expire_stale_artifact_reservations=lambda now, scope: 0,
    )
    stop = asyncio.Event()
    waits = []

    async def two_iterations(awaitable, *, timeout):
        waits.append(timeout)
        awaitable.close()
        if len(waits) == 2:
            stop.set()

    monkeypatch.setattr(scheduler_module.asyncio, "wait_for", two_iterations)
    scheduler = scheduler_module.RecoveryScheduler(
        RecoveryService(repo, SystemRecoveryScope("failure-isolation")),
        SimpleNamespace(
            try_acquire=lambda: SimpleNamespace(release=lambda: None)
        ),
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )

    await scheduler.run(stop)

    assert attempts == ["expire", "expire"]
    assert waits == [2.0, 2.0]
    assert scheduler.last_summary is not None
    assert scheduler.last_summary.skipped_locked is False
    assert scheduler.last_failure is not None
    assert (
        scheduler.last_failure.error_type,
        scheduler.last_failure.error_message,
    ) == ("OSError", "database unavailable")


@pytest.mark.anyio
async def test_cancelling_async_sweep_still_releases_lock_after_worker_unwinds():
    """Cancellation must not strand a session advisory lock in the worker thread."""
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope
    from odp_api.processes.recovery_scheduler import RecoveryScheduler

    started = threading.Event()
    permit_finish = threading.Event()
    released = threading.Event()

    def block(now, scope):
        started.set()
        assert permit_finish.wait(timeout=2)
        return 0

    repo = SimpleNamespace(
        expire_leases=block,
        release_due_retries=lambda now, scope: 0,
        redispatch_stale_ready=lambda now, scope: 0,
        release_expired_outbox_claims=lambda now, scope: 0,
        count_quarantined_messages=lambda now, scope: 0,
        expire_stale_artifact_reservations=lambda now, scope: 0,
    )
    scheduler = RecoveryScheduler(
        RecoveryService(repo, SystemRecoveryScope("cancel-release")),
        SimpleNamespace(
            try_acquire=lambda: SimpleNamespace(release=released.set)
        ),
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )

    task = asyncio.create_task(scheduler.run_once_async())
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    permit_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(released.wait, 1)


@pytest.mark.anyio
async def test_repeated_cancellation_waits_for_lock_release_before_task_finishes():
    """A second cancel must not let the outer Task outrun the lock-owning thread."""
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope
    from odp_api.processes.recovery_scheduler import RecoveryScheduler

    started = threading.Event()
    permit_finish = threading.Event()
    released = threading.Event()

    def block(now, scope):
        started.set()
        assert permit_finish.wait(timeout=2)
        return 0

    repo = SimpleNamespace(
        expire_leases=block,
        release_due_retries=lambda now, scope: 0,
        redispatch_stale_ready=lambda now, scope: 0,
        release_expired_outbox_claims=lambda now, scope: 0,
        count_quarantined_messages=lambda now, scope: 0,
        expire_stale_artifact_reservations=lambda now, scope: 0,
    )
    scheduler = RecoveryScheduler(
        RecoveryService(repo, SystemRecoveryScope("double-cancel-release")),
        SimpleNamespace(
            try_acquire=lambda: SimpleNamespace(release=released.set)
        ),
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )

    task = asyncio.create_task(scheduler.run_once_async())
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    # Let the first CancelledError reach run_once_async's drain path before
    # issuing another cancellation request against that in-flight drain.
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)

    assert task.done() is False
    assert released.is_set() is False
    permit_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(released.wait, 1)
