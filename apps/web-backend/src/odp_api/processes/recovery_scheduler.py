"""Independent PostgreSQL recovery scheduler with session advisory locking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import Connection, Engine, text

from odp_api.modules.tasks.recovery import RecoveryService, RecoverySummary

LOGGER = logging.getLogger(__name__)
DEFAULT_RECOVERY_INTERVAL_SECONDS = 2.0
# One stable signed-bigint key shared by every deployed recovery scheduler.
RECOVERY_ADVISORY_LOCK_KEY = 0x4F445031


class AdvisoryLockLease(Protocol):
    """One acquired session-level lock held until explicitly released."""

    backend_pid: int | None

    def release(self) -> None: ...


class AdvisoryLockPort(Protocol):
    """Nonblocking scheduler-leader election boundary."""

    def try_acquire(self) -> AdvisoryLockLease | None: ...


@dataclass(frozen=True, slots=True)
class RecoverySchedulerSummary:
    """Structured outcome from one attempted scheduler sweep."""

    skipped_locked: bool
    recovery: RecoverySummary | None


@dataclass(frozen=True, slots=True)
class RecoverySchedulerFailure:
    """Bounded structured details for the most recent failed iteration."""

    error_type: str
    error_message: str


class _PostgreSqlAdvisoryLease:
    """Keep the checked-out PostgreSQL connection pinned for the lock lifetime."""

    def __init__(self, connection: Connection, key: int, backend_pid: int) -> None:
        self._connection = connection
        self._key = key
        self.backend_pid = backend_pid
        self._state_lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        """Unlock on the same PostgreSQL session and close it exactly once."""

        with self._state_lock:
            if self._released:
                return
            self._released = True
            try:
                unlocked = self._connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                ).scalar_one()
                if unlocked is not True:
                    raise RuntimeError("PostgreSQL recovery advisory lock was not held")
            finally:
                self._connection.close()


class PostgreSqlAdvisoryLock:
    """Acquire a session-level advisory lock without waiting."""

    def __init__(
        self, engine: Engine, *, key: int = RECOVERY_ADVISORY_LOCK_KEY
    ) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgreSqlAdvisoryLock requires a PostgreSQL engine")
        if not isinstance(key, int) or isinstance(key, bool):
            raise TypeError("PostgreSQL advisory lock key must be an integer")
        if key < -(2**63) or key >= 2**63:
            raise ValueError("PostgreSQL advisory lock key must fit signed BIGINT")
        self._engine = engine
        self._key = key

    def try_acquire(self) -> AdvisoryLockLease | None:
        """Return a pinned lease, or ``None`` when another session owns the key."""

        connection = self._engine.connect()
        try:
            row = connection.execute(
                text(
                    "SELECT pg_backend_pid() AS backend_pid, "
                    "pg_try_advisory_lock(:key) AS acquired"
                ),
                {"key": self._key},
            ).one()
            acquired = row.acquired
            if acquired is not True:
                connection.close()
                return None
            return _PostgreSqlAdvisoryLease(
                connection, self._key, int(row.backend_pid)
            )
        except BaseException:
            connection.close()
            raise


class RecoveryScheduler:
    """Run the complete PostgreSQL recovery sweep under one session lock."""

    def __init__(
        self,
        recovery: RecoveryService,
        advisory_lock: AdvisoryLockPort,
        *,
        clock: Callable[[], datetime],
        interval_seconds: float = DEFAULT_RECOVERY_INTERVAL_SECONDS,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be a zero-argument callable")
        if interval_seconds <= 0:
            raise ValueError("recovery interval must be positive")
        self._recovery = recovery
        self._advisory_lock = advisory_lock
        self._clock = clock
        self.interval_seconds = float(interval_seconds)
        self.last_summary: RecoverySchedulerSummary | None = None
        self.last_failure: RecoverySchedulerFailure | None = None

    def run_once(self) -> RecoverySchedulerSummary:
        """Attempt one all-or-nothing leadership window for the database sweep."""

        lease = self._advisory_lock.try_acquire()
        if lease is None:
            summary = RecoverySchedulerSummary(skipped_locked=True, recovery=None)
            self.last_summary = summary
            return summary

        try:
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError("recovery clock must return a timezone-aware datetime")
            recovery = self._recovery.run_once(now.astimezone(UTC))
            summary = RecoverySchedulerSummary(skipped_locked=False, recovery=recovery)
            self.last_summary = summary
            return summary
        finally:
            lease.release()

    async def run_once_async(self) -> RecoverySchedulerSummary:
        """Offload synchronous SQLAlchemy work and drain it before cancellation exits."""

        worker = asyncio.create_task(asyncio.to_thread(self.run_once))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # ``to_thread`` cannot stop an in-flight database sweep.  Awaiting the
            # shielded worker guarantees its finally block releases the session
            # advisory lock before cancellation leaves this process boundary.
            # Repeated cancellation requests must not interrupt this drain.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            with contextlib.suppress(Exception):
                worker.result()
            raise

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Run every two seconds, isolating one failed iteration."""

        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                await self.run_once_async()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_failure = RecoverySchedulerFailure(
                    error_type=type(error).__name__,
                    error_message=str(error)[:1024],
                )
                LOGGER.exception("database recovery scheduler iteration failed")

            if stop.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)


__all__ = [
    "DEFAULT_RECOVERY_INTERVAL_SECONDS",
    "RECOVERY_ADVISORY_LOCK_KEY",
    "AdvisoryLockLease",
    "AdvisoryLockPort",
    "PostgreSqlAdvisoryLock",
    "RecoveryScheduler",
    "RecoverySchedulerFailure",
    "RecoverySchedulerSummary",
]
