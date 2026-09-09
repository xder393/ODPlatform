from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)
from typing_extensions import TypeAliasType

JsonValue = TypeAliasType(
    "JsonValue",
    "None | StrictBool | StrictInt | StrictFloat | StrictStr | list[JsonValue] | dict[StrictStr, JsonValue]",
)


class InspectionAlert(BaseModel):
    """A defect alert emitted by an inspection camera."""

    event_id: UUID
    organization_id: UUID
    camera_id: UUID
    occurred_at: datetime
    defect_class: str
    confidence: float = Field(ge=0, le=1)


class InspectionAlertCreated(BaseModel):
    """Frozen durable envelope for an inspection alert outbox event."""

    alert_id: UUID
    organization_id: UUID
    case_id: UUID
    event_id: UUID
    camera_id: UUID
    line_id: UUID
    defect_type: str
    severity: str
    confidence: float = Field(ge=0, le=1)
    occurred_at: datetime
    business_cursor: str


class EventEnvelope(BaseModel):
    """Versioned, JSON-only envelope transported by the event distribution plane.

    The envelope contains identifiers and a small reference payload.  Payloads
    are deliberately typed as JSON values so binary handles, ORM objects, and
    other process-local values cannot leak into Redis.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    event_id: UUID
    event_type: str
    schema_version: int = Field(ge=1)
    occurred_at: datetime
    correlation_id: UUID
    organization_id: UUID
    aggregate_id: UUID
    traceparent: str | None = None
    tracestate: str | None = None
    payload: dict[str, JsonValue]

    @field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime) -> datetime:
        """Represent timestamps in UTC, treating legacy naive values as UTC."""

        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_serializer("occurred_at")
    def _serialize_occurred_at(self, value: datetime) -> str:
        return _rfc3339(value)

    @model_validator(mode="after")
    def _validate_inference_payload(self) -> EventEnvelope:
        if self.event_type != "vision.inference.requested.v1":
            return self
        if set(self.payload) != {"task_id", "dispatch_seq"}:
            raise ValueError(
                "inference envelope payload must contain only task_id and dispatch_seq"
            )
        task_id = self.payload["task_id"]
        dispatch_seq = self.payload["dispatch_seq"]
        if not isinstance(task_id, str):
            raise ValueError("inference task_id must be a UUID string")
        try:
            UUID(task_id)
        except ValueError as exc:
            raise ValueError("inference task_id must be a UUID string") from exc
        if type(dispatch_seq) is not int or dispatch_seq < 1:  # noqa: E721 - reject bool explicitly
            raise ValueError("inference dispatch_seq must be a positive integer")
        return self

    def canonical_json(self) -> str:
        """Return deterministic compact JSON suitable for a Redis stream field."""

        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _rfc3339(value: datetime) -> str:
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")
