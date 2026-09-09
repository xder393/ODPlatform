"""Redis Streams publisher for transactional-outbox envelopes.

Redis is only the distribution plane.  The publisher therefore writes one
canonical JSON envelope as a string field and leaves retention to the
dedicated stream-retention process; no ``MAXLEN`` option is sent here.
"""

from __future__ import annotations

from typing import Final, Protocol

from odp_schemas.events import EventEnvelope, InspectionAlertCreated

from odp_api.ports.events import OutboxPublisherPort, UnknownEventType

EVENT_DESTINATIONS: dict[str, str] = {
    "vision.inference.requested.v1": "odp:inference:tasks",
    "inspection.alert.created.v1": "odp:inspection:alerts",
}
MAX_ENVELOPE_BYTES: Final = 64 * 1024


class RedisEventStreamClient(Protocol):
    """Small synchronous Redis command surface required by the relay."""

    def xadd(self, stream: str, fields: dict[str, str]) -> object: ...


class RedisOutboxPublisher(OutboxPublisherPort):
    """Publish allowlisted envelopes with unbounded ``XADD``."""

    def __init__(
        self,
        client: RedisEventStreamClient,
        destinations: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._destinations = dict(
            EVENT_DESTINATIONS if destinations is None else destinations
        )

    def publish(self, envelope: EventEnvelope) -> str:
        try:
            destination = self._destinations[envelope.event_type]
        except KeyError as exc:
            raise UnknownEventType(envelope.event_type) from exc

        outbound_envelope = envelope
        if envelope.event_type == "inspection.alert.created.v1":
            alert = InspectionAlertCreated.model_validate(envelope.payload)
            if alert.organization_id != envelope.organization_id:
                raise ValueError("alert envelope tenant does not match envelope tenant")
            alert_payload = alert.model_dump(mode="json")
            if set(alert_payload) != set(envelope.payload):
                raise ValueError("alert envelope payload is outside the P1A contract")
            outbound_envelope = envelope.model_copy(update={"payload": alert_payload})

        canonical = outbound_envelope.canonical_json()
        if len(canonical.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ValueError("canonical event envelope exceeds 64 KiB")

        # Keep the event as one text field.  In particular, do not send object
        # keys or bytes and do not apply approximate stream trimming here.
        message_id = self._client.xadd(destination, {"envelope": canonical})
        return _decode_message_id(message_id)


def _decode_message_id(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


__all__ = [
    "EVENT_DESTINATIONS",
    "MAX_ENVELOPE_BYTES",
    "RedisEventStreamClient",
    "RedisOutboxPublisher",
]
