import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

BACKEND_SRC = Path(__file__).parents[3] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[5] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(SHARED_SCHEMAS_SRC), str(BACKEND_SRC)]

from odp_schemas.events import EventEnvelope, InspectionAlertCreated

from odp_api.adapters.events.redis_streams import (
    EVENT_DESTINATIONS,
    RedisOutboxPublisher,
)
from odp_api.ports.events import UnknownEventType


def _envelope(event_type: str = "vision.inference.requested.v1", **payload):
    organization_id = (
        UUID(payload["organization_id"])
        if event_type == "inspection.alert.created.v1" and "organization_id" in payload
        else uuid4()
    )
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        schema_version=1,
        occurred_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        correlation_id=uuid4(),
        organization_id=organization_id,
        aggregate_id=uuid4(),
        payload=payload or {"task_id": str(uuid4()), "dispatch_seq": 2},
    )


def test_inference_event_contains_only_reference_fields():
    envelope = EventEnvelope.model_validate(
        {
            "event_id": str(uuid4()),
            "event_type": "vision.inference.requested.v1",
            "schema_version": 1,
            "occurred_at": "2026-08-25T12:00:00Z",
            "correlation_id": str(uuid4()),
            "organization_id": str(uuid4()),
            "aggregate_id": str(uuid4()),
            "payload": {"task_id": str(uuid4()), "dispatch_seq": 2},
        }
    )

    body = envelope.model_dump(mode="json")

    assert set(body["payload"]) == {"task_id", "dispatch_seq"}
    assert "object_key" not in str(body)


def test_inference_event_rejects_non_reference_payload_fields():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {
                "event_id": str(uuid4()),
                "event_type": "vision.inference.requested.v1",
                "schema_version": 1,
                "occurred_at": "2026-08-25T12:00:00Z",
                "correlation_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "aggregate_id": str(uuid4()),
                "payload": {
                    "task_id": str(uuid4()),
                    "dispatch_seq": 2,
                    "object_key": "private/frame.jpg",
                },
            }
        )


def test_event_envelope_canonical_json_is_stable_and_rfc3339():
    envelope = _envelope(
        "test.event.v1",
        task_id=str(uuid4()),
        dispatch_seq=2,
        nested={"z": 1, "a": True},
    )

    assert envelope.canonical_json() == envelope.canonical_json()
    assert envelope.canonical_json().startswith('{"aggregate_id":')
    assert '"occurred_at":"2026-08-25T12:00:00Z"' in envelope.canonical_json()
    assert " " not in envelope.canonical_json()


def test_alert_envelope_reuses_numeric_schema_and_canonical_payload():
    alert = InspectionAlertCreated(
        alert_id=uuid4(),
        organization_id=uuid4(),
        case_id=uuid4(),
        event_id=uuid4(),
        camera_id=uuid4(),
        line_id=uuid4(),
        defect_type="scratch",
        severity="HIGH",
        confidence=0.91,
        occurred_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        business_cursor="42",
    )
    envelope = EventEnvelope(
        event_id=alert.event_id,
        event_type="inspection.alert.created.v1",
        schema_version=1,
        occurred_at=alert.occurred_at,
        correlation_id=alert.event_id,
        organization_id=alert.organization_id,
        aggregate_id=alert.alert_id,
        payload=alert.model_dump(mode="json"),
    )

    assert envelope.schema_version == 1
    assert envelope.model_dump(mode="json")["payload"] == alert.model_dump(mode="json")


def test_envelope_rejects_non_json_values_and_non_positive_schema_version():
    with pytest.raises(ValidationError):
        EventEnvelope(
            event_id=uuid4(),
            event_type="vision.inference.requested.v1",
            schema_version=0,
            occurred_at=datetime.now(UTC),
            correlation_id=uuid4(),
            organization_id=uuid4(),
            aggregate_id=uuid4(),
            payload={"bad": object()},
        )

    with pytest.raises(ValidationError):
        EventEnvelope(
            event_id=uuid4(),
            event_type="test.event.v1",
            schema_version=1,
            occurred_at=datetime.now(UTC),
            correlation_id=uuid4(),
            organization_id=uuid4(),
            aggregate_id=uuid4(),
            payload={"binary": b"secret"},
        )


class FakeRedis:
    def __init__(self):
        self.calls = []

    def xadd(self, stream, fields):
        self.calls.append((stream, fields))
        return "1-0"


def test_publisher_routes_both_allowlisted_events_without_maxlen():
    redis = FakeRedis()
    publisher = RedisOutboxPublisher(redis)

    for event_type in EVENT_DESTINATIONS:
        publisher.publish(
            _envelope(
                event_type,
                **(
                    {"task_id": str(uuid4()), "dispatch_seq": 1}
                    if event_type == "vision.inference.requested.v1"
                    else {
                        "alert_id": str(uuid4()),
                        "organization_id": str(uuid4()),
                        "case_id": str(uuid4()),
                        "event_id": str(uuid4()),
                        "camera_id": str(uuid4()),
                        "line_id": str(uuid4()),
                        "defect_type": "scratch",
                        "severity": "HIGH",
                        "confidence": 0.9,
                        "occurred_at": "2026-08-25T12:00:00Z",
                        "business_cursor": "1",
                    }
                ),
            )
        )

    assert [stream for stream, _fields in redis.calls] == list(
        EVENT_DESTINATIONS.values()
    )
    assert all("MAXLEN" not in str(call) for call in redis.calls)
    assert all(set(fields) == {"envelope"} for _stream, fields in redis.calls)
    assert all(isinstance(fields["envelope"], str) for _stream, fields in redis.calls)


def test_publisher_rejects_oversized_envelope_before_xadd():
    redis = FakeRedis()
    publisher = RedisOutboxPublisher(redis)
    envelope = EventEnvelope(
        event_id=uuid4(),
        event_type="vision.inference.requested.v1",
        schema_version=1,
        occurred_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        correlation_id=uuid4(),
        organization_id=uuid4(),
        aggregate_id=uuid4(),
        traceparent="x" * (64 * 1024),
        payload={"task_id": str(uuid4()), "dispatch_seq": 1},
    )

    assert len(envelope.canonical_json().encode("utf-8")) > 64 * 1024
    with pytest.raises(ValueError, match="64 KiB"):
        publisher.publish(envelope)

    assert redis.calls == []


def test_publisher_rejects_unknown_event_type_before_redis_call():
    redis = FakeRedis()
    publisher = RedisOutboxPublisher(redis)

    with pytest.raises(UnknownEventType):
        publisher.publish(_envelope("unknown.event.v1", value="bad"))

    assert redis.calls == []


def test_publisher_rejects_alert_payload_outside_the_p1a_contract():
    redis = FakeRedis()
    publisher = RedisOutboxPublisher(redis)
    payload = {
        "alert_id": str(uuid4()),
        "organization_id": str(uuid4()),
        "case_id": str(uuid4()),
        "event_id": str(uuid4()),
        "camera_id": str(uuid4()),
        "line_id": str(uuid4()),
        "defect_type": "scratch",
        "severity": "HIGH",
        "confidence": 0.9,
        "occurred_at": "2026-08-25T12:00:00Z",
        "business_cursor": "1",
        "object_key": "private/frame.jpg",
    }
    envelope = _envelope("inspection.alert.created.v1", **payload)

    with pytest.raises(ValueError):
        publisher.publish(envelope)

    assert redis.calls == []


def test_publisher_emits_validated_alert_payload_as_canonical_json():
    redis = FakeRedis()
    publisher = RedisOutboxPublisher(redis)
    payload = {
        "alert_id": str(uuid4()),
        "organization_id": str(uuid4()),
        "case_id": str(uuid4()),
        "event_id": str(uuid4()),
        "camera_id": str(uuid4()),
        "line_id": str(uuid4()),
        "defect_type": "scratch",
        "severity": "HIGH",
        "confidence": True,
        "occurred_at": "2026-08-25T12:00:00Z",
        "business_cursor": "1",
    }
    envelope = _envelope("inspection.alert.created.v1", **payload)

    publisher.publish(envelope)

    emitted = json.loads(redis.calls[0][1]["envelope"])
    assert emitted["payload"]["confidence"] == 1.0
    assert isinstance(emitted["payload"]["confidence"], float)
