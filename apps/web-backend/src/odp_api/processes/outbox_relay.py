"""Generic transactional-Outbox Relay process.

The relay owns only the delivery choreography: claim durable rows, publish
outside the claim transaction, and then mark successful publication in a
guarded transaction.  It deliberately does not know Redis destination names;
the publisher adapter owns the stable event-type allowlist.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from odp_schemas.events import EventEnvelope, InspectionAlertCreated

from odp_api.ports.events import (
    ClaimedOutboxEvent,
    OutboxPublisherPort,
    OutboxRepositoryPort,
)
from odp_api.processes.common import (
    install_signal_stop_event,
    load_settings,
    run_loop,
    utc_now,
)
from odp_api.settings import RelaySettings

DEFAULT_BATCH_SIZE: Final = 100
DEFAULT_CLAIM_LEASE: Final = timedelta(seconds=30)
DEFAULT_BACKOFF_BASE: Final = timedelta(seconds=1)
DEFAULT_BACKOFF_CAP: Final = timedelta(minutes=5)
DEFAULT_RELAY_INTERVAL_SECONDS: Final = 1.0


@dataclass(frozen=True, slots=True)
class RelayBatchResult:
    """Counts produced by one relay scan."""

    claimed: int = 0
    published: int = 0
    failed: int = 0
    mark_conflicts: int = 0

    @property
    def attempted(self) -> int:
        return self.published + self.failed + self.mark_conflicts

    @property
    def claimed_count(self) -> int:
        return self.claimed

    @property
    def published_count(self) -> int:
        return self.published

    @property
    def failed_count(self) -> int:
        return self.failed


class OutboxRelay:
    """Relay claimed Outbox events to an allowlisted publisher."""

    def __init__(
        self,
        repository: OutboxRepositoryPort,
        publisher: OutboxPublisherPort,
        *,
        relay_id: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        claim_lease: timedelta = DEFAULT_CLAIM_LEASE,
        backoff_base: timedelta = DEFAULT_BACKOFF_BASE,
        backoff_cap: timedelta = DEFAULT_BACKOFF_CAP,
    ) -> None:
        if relay_id is None:
            relay_id = f"outbox-relay-{uuid4()}"
        if not relay_id.strip():
            raise ValueError("relay_id is required")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if claim_lease <= timedelta(0):
            raise ValueError("claim_lease must be positive")
        if backoff_base <= timedelta(0):
            raise ValueError("backoff_base must be positive")
        if backoff_cap < backoff_base:
            raise ValueError("backoff_cap must be at least backoff_base")
        self._repository = repository
        self._publisher = publisher
        self._relay_id = relay_id
        self._batch_size = batch_size
        self._claim_lease = claim_lease
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    def run_batch(self, limit: int, now: datetime) -> RelayBatchResult:
        """Claim and attempt at most the configured number of ready events.

        A publisher exception is persisted as retry state.  A mark-published
        exception intentionally escapes: Redis may already contain the event,
        and allowing the claim lease to expire is what gives the next scan the
        required at-least-once duplicate replay semantics.
        """

        if limit < 1:
            return RelayBatchResult()
        current = _utc(now)
        claimed = tuple(
            self._repository.claim_ready(
                min(limit, self._batch_size),
                current,
                self._relay_id,
                self._claim_lease,
            )
        )
        published = failed = mark_conflicts = 0
        for event in claimed:
            try:
                envelope = _envelope_from_outbox(event)
                self._publisher.publish(envelope)
            except Exception as exc:  # noqa: BLE001 - all publish errors are retryable
                # The row remains durable.  The guarded update can be a no-op
                # when another relay reclaimed the expired lease in parallel.
                self._repository.mark_publish_failed(
                    event.outbox_id,
                    event.organization_id,
                    self._relay_id,
                    current,
                    _error_text(exc),
                    self._retry_delay(event.publish_attempts),
                )
                failed += 1
                continue

            marked = self._repository.mark_published(
                event.outbox_id,
                event.organization_id,
                self._relay_id,
                current,
            )
            if marked:
                published += 1
            else:
                mark_conflicts += 1
        return RelayBatchResult(len(claimed), published, failed, mark_conflicts)

    def _retry_delay(self, attempts_before_failure: int) -> timedelta:
        exponent = max(0, min(attempts_before_failure, 31))
        delay = self._backoff_base * (2**exponent)
        return min(delay, self._backoff_cap)


class OutboxRelayProcess:
    """Cooperative process loop around the synchronous relay transaction."""

    def __init__(
        self,
        relay: OutboxRelay,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        interval_seconds: float = DEFAULT_RELAY_INTERVAL_SECONDS,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if interval_seconds <= 0:
            raise ValueError("relay interval must be positive")
        self._relay = relay
        self._batch_size = batch_size
        self.interval_seconds = float(interval_seconds)
        self.last_result: RelayBatchResult | None = None

    async def run_once(self) -> RelayBatchResult:
        result = await asyncio.to_thread(self._relay.run_batch, self._batch_size, utc_now())
        self.last_result = result
        return result

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        await run_loop(self.run_once, interval_seconds=self.interval_seconds, stop_event=stop_event)


def _envelope_from_outbox(event: ClaimedOutboxEvent) -> EventEnvelope:
    """Build an envelope from durable row identity, not arbitrary payload data."""

    if event.event_type == "vision.inference.requested.v1":
        if not event.task_found or event.task_id is None or event.dispatch_seq is None:
            raise ValueError("inference event is missing task reference")
        # Inference payload is intentionally reference-only.  The task and
        # sequence columns are authoritative; an object key in a stale/mutated
        # JSON payload must never cross the distribution boundary.
        payload = {
            "task_id": str(event.task_id),
            "dispatch_seq": event.dispatch_seq,
        }
    elif event.event_type == "inspection.alert.created.v1":
        alert = InspectionAlertCreated.model_validate(event.payload)
        if alert.organization_id != event.organization_id:
            raise ValueError("alert event tenant does not match Outbox tenant")
        # Re-serializing through the P1A schema drops accidental transport
        # fields (for example an object key) while retaining its exact
        # canonical business payload.
        payload = alert.model_dump(mode="json")
    else:
        payload = dict(event.payload)

    correlation_id = event.correlation_id or _payload_uuid(
        event.payload, "correlation_id"
    )
    if correlation_id is None:
        # P1A does not persist correlation_id on OutboxEventRow.  The stable
        # Outbox identity is the safest deterministic fallback for this
        # required envelope field.
        correlation_id = event.outbox_id

    return EventEnvelope(
        event_id=event.outbox_id,
        event_type=event.event_type,
        schema_version=event.schema_version,
        occurred_at=event.occurred_at,
        correlation_id=correlation_id,
        organization_id=event.organization_id,
        aggregate_id=event.aggregate_id,
        traceparent=event.traceparent,
        tracestate=event.tracestate,
        payload=payload,
    )


def _payload_uuid(payload: object, key: str) -> UUID | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _error_text(error: Exception) -> str:
    text = str(error).strip()
    if not text:
        text = type(error).__name__
    # Error details are operational context, not a second payload channel.
    return f"{type(error).__name__}: {text[:1024]}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _build_process(settings: RelaySettings) -> OutboxRelayProcess:
    from odp_api.processes.runtime import build_relay

    return build_relay(settings)


def main() -> None:
    settings = load_settings(RelaySettings)
    process = _build_process(settings)

    async def serve() -> None:
        stop = install_signal_stop_event()
        await process.run(stop)

    asyncio.run(serve())


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_BACKOFF_CAP",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_CLAIM_LEASE",
    "DEFAULT_RELAY_INTERVAL_SECONDS",
    "OutboxRelay",
    "OutboxRelayProcess",
    "RelayBatchResult",
    "main",
]
