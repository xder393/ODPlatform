"""Read-only Redis wake-ups for the Realtime Gateway.

PostgreSQL/SQLAlchemy remains the alert fact source.  Redis entries carry only
the validated event envelope needed to identify a tenant and business cursor;
the adapter re-queries facts before yielding anything to a WebSocket and ACKs
the wake-up only after that delivery boundary has resumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import UUID

from odp_schemas.events import EventEnvelope, InspectionAlertCreated

from odp_api.ports.notifications import InspectionAlertFeedPort, StoredInspectionAlert

LOGGER = logging.getLogger(__name__)
ALERT_STREAM_NAME = "odp:inspection:alerts"
ALERT_GROUP_PREFIX = "odp-alert-gateway:"
READ_COUNT = 100
READ_BLOCK_MS = 15_000


class RedisGatewayInspectionAlertFeed:
    """Consume one short-lived Gateway consumer while facts stay in SQL."""

    def __init__(
        self,
        facts: InspectionAlertFeedPort,
        client: Any,
        *,
        instance_id: str,
        stream_name: str = ALERT_STREAM_NAME,
        async_client_factory: Any | None = None,
    ) -> None:
        if not instance_id.strip() or any(char.isspace() for char in instance_id):
            raise ValueError("instance_id must be a bounded non-empty token")
        if not callable(getattr(client, "async_client", None)) and async_client_factory is None:
            raise TypeError("Redis Gateway requires an async Redis client factory")
        self._facts = facts
        self._client = client
        self._stream = stream_name
        self._group = f"{ALERT_GROUP_PREFIX}{instance_id}"
        self._consumer = f"{instance_id}:{id(self)}"
        self._async_client_factory = async_client_factory or client.async_client

    def list(
        self,
        organization_id: UUID,
        after_cursor: str | None,
        limit: int,
        authorized_line_ids: frozenset[UUID] | None = None,
    ) -> Sequence[StoredInspectionAlert]:
        return self._facts.list(organization_id, after_cursor, limit, authorized_line_ids)

    async def subscribe(self, after_cursor: str | None) -> AsyncIterator[StoredInspectionAlert]:
        cursor = _parse_cursor(after_cursor)
        client = self._async_client_factory()
        await _ensure_group(client, self._stream, self._group)
        try:
            while True:
                response = await client.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer,
                    streams={self._stream: ">"},
                    count=READ_COUNT,
                    block=READ_BLOCK_MS,
                )
                for message_id, fields in _entries(response):
                    wakeup = _parse_wakeup(fields)
                    if wakeup is None:
                        # A malformed wake-up cannot be allowed to poison the
                        # Gateway PEL forever; REST remains the fact recovery
                        # path, so ACKing this hint is safe.
                        await client.xack(self._stream, self._group, message_id)
                        continue
                    organization_id, hinted_cursor = wakeup
                    query_after = str(min(cursor, hinted_cursor - 1))
                    facts = self._facts.list(organization_id, query_after, READ_COUNT)
                    for stored in facts:
                        if int(stored.cursor) <= cursor:
                            continue
                        cursor = int(stored.cursor)
                        yield stored
                    # The generator resumes only after the caller has had a
                    # chance to deliver the yielded fact to its local socket.
                    await client.xack(self._stream, self._group, message_id)
        finally:
            await _close_async_client(client)

    async def close(self) -> None:
        """Destroy this process's ephemeral group during graceful shutdown."""

        client = self._async_client_factory()
        try:
            await client.xgroup_destroy(self._stream, self._group)
        finally:
            await _close_async_client(client)


async def _ensure_group(client: Any, stream: str, group: str) -> None:
    try:
        await client.xgroup_create(stream, group, id="$", mkstream=True)
    except Exception as error:
        if "BUSYGROUP" not in str(error).upper():
            raise


def _entries(response: object) -> Sequence[tuple[str, Mapping[object, object]]]:
    if not response:
        return ()
    try:
        entries = response[-1][1]
    except (IndexError, KeyError, TypeError):
        return ()
    normalized: list[tuple[str, Mapping[object, object]]] = []
    if not isinstance(entries, Sequence):
        return ()
    for item in entries:
        if not isinstance(item, Sequence) or len(item) != 2 or not isinstance(item[1], Mapping):
            continue
        normalized.append((_decode(item[0]), item[1]))
    return tuple(normalized)


def _parse_wakeup(fields: Mapping[object, object]) -> tuple[UUID, int] | None:
    normalized = {_decode(key): _decode(value) for key, value in fields.items()}
    raw_envelope = normalized.get("envelope")
    if raw_envelope is None:
        # Development-only trigger compatibility.  Durable P1 effects use
        # the canonical EventEnvelope path above; this fallback is intentionally
        # bounded to the explicit cursor + tenant hint and still re-queries SQL.
        try:
            organization_id = UUID(normalized["organization_id"])
            cursor = int(normalized["cursor"])
            return (organization_id, cursor) if cursor > 0 else None
        except (KeyError, TypeError, ValueError):
            return None
    try:
        envelope = EventEnvelope.model_validate_json(raw_envelope)
        if envelope.event_type != "inspection.alert.created.v1":
            return None
        alert = InspectionAlertCreated.model_validate(envelope.payload)
        if alert.organization_id != envelope.organization_id:
            return None
        cursor = int(alert.business_cursor)
        if cursor < 1:
            return None
        return envelope.organization_id, cursor
    except (TypeError, ValueError, json.JSONDecodeError):
        LOGGER.warning("discarding malformed inspection alert wake-up")
        return None


def _parse_cursor(value: str | None) -> int:
    if value is None or value == "":
        return 0
    if not value.isdigit() or int(value) < 0:
        raise ValueError("invalid cursor")
    return int(value)


async def _close_async_client(client: Any) -> None:
    close = getattr(client, "aclose", None)
    if close is not None:
        result = close()
        if asyncio.iscoroutine(result):
            await result


def _decode(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


__all__ = ["ALERT_GROUP_PREFIX", "ALERT_STREAM_NAME", "RedisGatewayInspectionAlertFeed"]
