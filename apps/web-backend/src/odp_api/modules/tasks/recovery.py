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
    outbox_claims_released: int
    quarantined_messages: int
    artifact_reservations_expired: int


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
class DeadLetterReplayResult:
    """New task identity created when a terminal task is replayed."""

    task_id: UUID
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
    def release_due_retries(
        self, now: datetime, scope: SystemRecoveryScope, *, limit: int = 100
    ) -> int: ...
    def redispatch_stale_ready(
        self, now: datetime, scope: SystemRecoveryScope, *, limit: int = 100
    ) -> int: ...
    def expire_leases(
        self, now: datetime, scope: SystemRecoveryScope, *, limit: int = 100
    ) -> int: ...
    def release_expired_outbox_claims(
        self, now: datetime, scope: SystemRecoveryScope, *, limit: int = 100
    ) -> int: ...
    def count_quarantined_messages(
        self, now: datetime, scope: SystemRecoveryScope
    ) -> int: ...
    def expire_stale_artifact_reservations(
        self, now: datetime, scope: SystemRecoveryScope, *, limit: int = 100
    ) -> int: ...


class RecoveryService:
    def __init__(self, repository: RecoveryRepository, scope: SystemRecoveryScope) -> None:
        if not isinstance(scope, SystemRecoveryScope):
            raise TypeError("RecoveryService requires a SystemRecoveryScope")
        self._repository = repository
        self._scope = scope

    def run_once(self, now: datetime) -> RecoverySummary:
        # Preserve the P1A two-phase ordering: work already waiting/recoverable
        # is released first, while leases expired by this sweep become eligible
        # on the next two-second iteration rather than being redispatched in the
        # same control-plane pass.
        retries_released = self._repository.release_due_retries(now, self._scope)
        stale_ready_redispatched = self._repository.redispatch_stale_ready(
            now, self._scope
        )
        leases_expired = self._repository.expire_leases(now, self._scope)
        outbox_claims_released = self._repository.release_expired_outbox_claims(
            now, self._scope
        )
        quarantined_messages = self._repository.count_quarantined_messages(
            now, self._scope
        )
        artifact_reservations_expired = (
            self._repository.expire_stale_artifact_reservations(now, self._scope)
        )
        return RecoverySummary(
            retries_released=retries_released,
            stale_ready_redispatched=stale_ready_redispatched,
            leases_expired=leases_expired,
            outbox_claims_released=outbox_claims_released,
            quarantined_messages=quarantined_messages,
            artifact_reservations_expired=artifact_reservations_expired,
        )
