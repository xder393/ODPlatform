"""Database fact feed with Redis Streams as bounded cross-instance wake-ups."""

import asyncio
from contextlib import suppress
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from odp_api.adapters.notifications.sqlite_feed import SqliteInspectionAlertFeed
from odp_api.ports.notifications import StoredInspectionAlert
from odp_schemas.events import InspectionAlert


class RedisDurableInspectionAlertFeed:
    """Never reads reconciliation facts from Redis, only uses it for transport."""

    def __init__(self, facts: SqliteInspectionAlertFeed, client: Any, stream_name: str = "odp:inspection-alerts") -> None:
        if not callable(getattr(client, "xadd_bounded", None)) or not callable(getattr(client, "xread", None)):
            raise RuntimeError("Redis alert transport requires bounded XADD and blocking XREAD")
        self._facts, self._client, self._stream = facts, client, stream_name

    def publish(self, alert: InspectionAlert, line_id: UUID | None) -> str:
        cursor = self._facts.publish(alert, line_id)
        fields = {"cursor": cursor, "event_id": str(alert.event_id)}
        self._client.xadd_bounded(self._stream, fields, 10_000)
        return cursor

    def list(self, organization_id: UUID, after_cursor: str | None, limit: int, authorized_line_ids=None):
        return self._facts.list(organization_id, after_cursor, limit, authorized_line_ids)

    async def subscribe(self, after_cursor: str | None) -> AsyncIterator[StoredInspectionAlert]:
        if after_cursor is not None and (not after_cursor.isdigit() or int(after_cursor) < 0):
            raise ValueError("invalid cursor")
        cursor = int(after_cursor or "0")
        stream_cursor = "$"
        while True:
            for item in self._facts._list_after(cursor):
                cursor = int(item.cursor)
                yield item
            response = await asyncio.to_thread(self._client.xread, self._stream, stream_cursor, 15_000)
            stream_cursor = _last_stream_id(response) or stream_cursor
            await asyncio.sleep(0)


def _last_stream_id(response: object) -> str | None:
    if not response:
        return None
    try:
        # RESP shape: [[stream-name, [[entry-id, [field, value, ...]], ...]]]
        entries = response[-1][1]
        return entries[-1][0].decode() if isinstance(entries[-1][0], bytes) else str(entries[-1][0])
    except (IndexError, KeyError, TypeError):
        return None
