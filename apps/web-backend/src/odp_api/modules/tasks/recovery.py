"""Durable database recovery scheduler contracts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    retries_released: int
    stale_ready_redispatched: int
    leases_expired: int


@dataclass(frozen=True, slots=True)
class QuarantineResult:
    quarantine_id: UUID
    ack_after_commit: bool


@dataclass(frozen=True, slots=True)
class ReplayResult:
    replayed: bool
    dispatch_seq: int
    new_outbox_id: UUID


@dataclass(frozen=True, slots=True)
class SystemRecoveryScope:
    """Explicit capability held only by the database recovery scheduler."""

    scheduler_id: str

    def __post_init__(self) -> None:
        if not self.scheduler_id.strip():
            raise ValueError("scheduler_id is required")


class RecoveryRepository(Protocol):
    def release_due_retries(self, now: datetime, scope: SystemRecoveryScope) -> int: ...
    def redispatch_stale_ready(self, now: datetime, scope: SystemRecoveryScope) -> int: ...
    def expire_leases(self, now: datetime, scope: SystemRecoveryScope) -> int: ...


class RecoveryService:
    def __init__(self, repository: RecoveryRepository, scope: SystemRecoveryScope) -> None:
        if not isinstance(scope, SystemRecoveryScope):
            raise TypeError("RecoveryService requires a SystemRecoveryScope")
        self._repository = repository
        self._scope = scope

    def run_once(self, now: datetime) -> RecoverySummary:
        return RecoverySummary(
            retries_released=self._repository.release_due_retries(now, self._scope),
            stale_ready_redispatched=self._repository.redispatch_stale_ready(now, self._scope),
            leases_expired=self._repository.expire_leases(now, self._scope),
        )
