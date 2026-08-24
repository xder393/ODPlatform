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
        self._facts, self._client, self._stream = facts, client, stream_name

    def publish(self, alert: InspectionAlert, line_id: UUID | None) -> str:
        cursor = self._facts.publish(alert, line_id)
        fields = {"cursor": cursor, "event_id": str(alert.event_id)}
        bounded = getattr(self._client, "xadd_bounded", None)
        if bounded is None:
            self._client.xadd(self._stream, fields)
        else:
            bounded(self._stream, fields, 10_000)
        return cursor

    def list(self, organization_id: UUID, after_cursor: str | None, limit: int):
        return self._facts.list(organization_id, after_cursor, limit)

    async def subscribe(self, after_cursor: str | None) -> AsyncIterator[StoredInspectionAlert]:
        # The durable delegate re-queries after its bounded timeout. Redis
        # messages reduce cross-instance latency; a lost stream is never used
        # as evidence that a database fact does not exist.
        reader = getattr(self._client, "xread", None)
        task = (
            asyncio.create_task(self._read_wakeups(reader))
            if reader is not None
            else None
        )
        try:
            async for item in self._facts.subscribe(after_cursor):
                yield item
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _read_wakeups(self, reader: Any) -> None:
        """Block on bounded Redis XREAD; durable queries decide what to send."""
        stream_cursor = "$"
        while True:
            response = await asyncio.to_thread(reader, self._stream, stream_cursor, 15_000)
            stream_cursor = _last_stream_id(response) or stream_cursor


def _last_stream_id(response: object) -> str | None:
    if not response:
        return None
    try:
        # RESP shape: [[stream-name, [[entry-id, [field, value, ...]], ...]]]
        entries = response[-1][1]
        return entries[-1][0].decode() if isinstance(entries[-1][0], bytes) else str(entries[-1][0])
    except (IndexError, KeyError, TypeError):
        return None
