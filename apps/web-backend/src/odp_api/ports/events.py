"""Ports for versioned transactional-outbox event distribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from odp_schemas.events import EventEnvelope


class UnknownEventType(ValueError):
    """Raised when an event has no explicitly configured distribution target."""


class OutboxPublisherPort(Protocol):
    """Publishes one validated event envelope to the configured transport."""

    def publish(self, envelope: EventEnvelope) -> str: ...


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    """Detached, tenant-scoped facts returned after an Outbox claim commit."""

    outbox_id: UUID
    organization_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    task_id: UUID | None
    dispatch_seq: int | None
    event_type: str
    schema_version: int
    payload: Mapping[str, object]
    occurred_at: datetime
    publish_attempts: int = 0
    correlation_id: UUID | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    task_found: bool = True


class OutboxRepositoryPort(Protocol):
    """System-scoped persistence boundary used by the Outbox Relay.

    Implementations must commit claims before returning and perform the
    publish-success/failure updates in separate guarded transactions.
    """

    def claim_ready(
        self,
        limit: int,
        now: datetime,
        claim_owner: str,
        claim_lease: timedelta,
    ) -> Sequence[ClaimedOutboxEvent]: ...

    def mark_published(
        self,
        outbox_id: UUID,
        organization_id: UUID,
        claim_owner: str,
        published_at: datetime,
    ) -> bool: ...

    def mark_publish_failed(
        self,
        outbox_id: UUID,
        organization_id: UUID,
        claim_owner: str,
        failed_at: datetime,
        error: str,
        retry_delay: timedelta,
    ) -> bool: ...
