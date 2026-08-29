"""Redis Stream implementation of the inspection-alert transport ports."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from odp_schemas.events import InspectionAlert

from odp_api.ports.notifications import StoredInspectionAlert


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
        self, organization_id: UUID, after_cursor: str | datetime | None = None, limit: int = 100
    ) -> list[StoredInspectionAlert]:
        alerts: list[StoredInspectionAlert] = []
        for stream_cursor, fields in self._client.xrange(self._stream_name):
            stored = _stored_alert(stream_cursor, fields)
            if stored.alert.organization_id != organization_id:
                continue
            if isinstance(after_cursor, datetime) and stored.updated_at <= _as_utc(after_cursor):
                continue
            if isinstance(after_cursor, str) and stored.cursor <= after_cursor:
                continue
            alerts.append(stored)
        return alerts[:limit]


def _stored_alert(cursor: str, fields: Mapping[str | bytes, str | bytes]) -> StoredInspectionAlert:
    normalized = {
        _decode(key): _decode(value)
        for key, value in fields.items()
    }
    alert = InspectionAlert.model_validate_json(normalized["alert"])
    line_id = normalized.get("line_id")
    return StoredInspectionAlert(
        cursor=cursor,
        alert=alert,
        updated_at=_as_utc(datetime.fromisoformat(normalized["updated_at"])),
        line_id=UUID(line_id) if line_id else None,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _decode(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value
