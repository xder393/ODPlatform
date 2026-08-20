"""Redis Stream implementation of the inspection-alert transport ports."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from odp_api.ports.notifications import StoredInspectionAlert

from odp_schemas.events import InspectionAlert


class RedisInspectionAlertClient(Protocol):
    def xadd(self, stream: str, fields: dict[str, str]) -> object: ...

    def xadd_once(
        self, stream: str, claim_key: str, fields: dict[str, str]
    ) -> object: ...

    def xrange(
        self, stream: str
    ) -> Sequence[tuple[str, Mapping[str | bytes, str | bytes]]]: ...


class RedisStreamInspectionAlertFeed:
    """Uses an append-only stream while keeping router reads provider-neutral."""

    def __init__(
        self, client: RedisInspectionAlertClient, stream_name: str = "odp:inspection-alerts"
    ) -> None:
        self._client = client
        self._stream_name = stream_name

    def publish(self, alert: InspectionAlert, line_id: UUID | None) -> None:
        fields = {
            "alert": alert.model_dump_json(),
            "updated_at": alert.occurred_at.isoformat(),
            "line_id": str(line_id) if line_id is not None else "",
        }
        append_once = getattr(self._client, "xadd_once", None)
        if append_once is not None:
            append_once(
                self._stream_name,
                f"{self._stream_name}:event:{alert.event_id}",
                fields,
            )
            return
        self._client.xadd(self._stream_name, fields)

    def list(
        self, organization_id: UUID, updated_after: datetime | None = None
    ) -> list[StoredInspectionAlert]:
        alerts: list[StoredInspectionAlert] = []
        for _, fields in self._client.xrange(self._stream_name):
            stored = _stored_alert(fields)
            if stored.alert.organization_id != organization_id:
                continue
            if updated_after is not None and stored.updated_at <= _as_utc(updated_after):
                continue
            alerts.append(stored)
        return alerts


def _stored_alert(fields: Mapping[str | bytes, str | bytes]) -> StoredInspectionAlert:
    normalized = {
        _decode(key): _decode(value)
        for key, value in fields.items()
    }
    alert = InspectionAlert.model_validate_json(normalized["alert"])
    line_id = normalized.get("line_id")
    return StoredInspectionAlert(
        alert=alert,
        updated_at=_as_utc(datetime.fromisoformat(normalized["updated_at"])),
        line_id=UUID(line_id) if line_id else None,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _decode(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value
