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


class RecoveryRepository(Protocol):
    def release_due_retries(self, now: datetime) -> int: ...
    def redispatch_stale_ready(self, now: datetime, organization_id: UUID | None = None) -> int: ...
    def expire_leases(self, now: datetime, organization_id: UUID | None = None) -> int: ...


class RecoveryService:
    def __init__(self, repository: RecoveryRepository) -> None:
        self._repository = repository

    def run_once(self, now: datetime) -> RecoverySummary:
        return RecoverySummary(
            retries_released=self._repository.release_due_retries(now),
            stale_ready_redispatched=self._repository.redispatch_stale_ready(now),
            leases_expired=self._repository.expire_leases(now),
        )
