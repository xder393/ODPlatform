"""Redis Streams delivery for PostgreSQL-owned inference tasks.

The consumer is deliberately a transport-only adapter.  Redis entries carry
the canonical event envelope; task state, artifact metadata, and execution
authority remain in PostgreSQL and are resolved by :mod:`inference_worker`.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import uuid4

STREAM_NAME: Final = "odp:inference:tasks"
GROUP_NAME: Final = "odp-inference-workers"
READ_COUNT: Final = 10
READ_BLOCK_MS: Final = 2_000
CLAIM_IDLE_MS: Final = 20_000
MAX_ENVELOPE_BYTES: Final = 64 * 1024


class RedisInferenceClient(Protocol):
    """Async Redis command subset used by the inference consumer."""

    async def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str,
        mkstream: bool,
    ) -> object: ...

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        count: int,
        block: int,
    ) -> object: ...

    async def xautoclaim(
        self,
        *,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        count: int,
    ) -> object: ...

    async def xack(self, name: str, groupname: str, *ids: str) -> object: ...


@dataclass(frozen=True, slots=True)
class RedisInferenceMessage:
    """A normalized Redis Stream entry returned to an inference Worker."""

    message_id: str
    fields: Mapping[str, object]
    stream: str = STREAM_NAME

    @property
    def envelope(self) -> object:
        """Return the canonical envelope field, or all fields if it is absent."""

        return self.fields.get("envelope", self.fields)

    @property
    def payload(self) -> object:
        """Compatibility alias for callers that call the envelope payload."""

        return self.envelope

    @property
    def raw_payload(self) -> bytes:
        """Encode a message payload without allowing arbitrary binary expansion."""

        value = self.envelope
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8", errors="surrogatepass")
        if isinstance(value, Mapping):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        return str(value).encode("utf-8", errors="replace")


# A shorter name is useful at the Worker boundary and keeps the transport
# object discoverable for callers that do not need the implementation prefix.
InferenceMessage = RedisInferenceMessage


class RedisInferenceConsumer:
    """Read and recover inference messages from one Redis consumer group.

    ``consumer_name`` is generated once during construction.  It therefore
    identifies the process for its entire lifetime, including every
    ``XAUTOCLAIM`` call made by that process.
    """

    def __init__(
        self,
        client: RedisInferenceClient,
        *,
        stream_name: str = STREAM_NAME,
        group_name: str = GROUP_NAME,
        consumer_name: str | None = None,
        batch_size: int = READ_COUNT,
        block_ms: int = READ_BLOCK_MS,
        claim_idle_ms: int = CLAIM_IDLE_MS,
    ) -> None:
        if not stream_name.strip():
            raise ValueError("stream_name is required")
        if not group_name.strip():
            raise ValueError("group_name is required")
        if consumer_name is None:
            consumer_name = str(uuid4())
        if not consumer_name.strip():
            raise ValueError("consumer_name is required")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if block_ms < 0:
            raise ValueError("block_ms cannot be negative")
        if claim_idle_ms < 1:
            raise ValueError("claim_idle_ms must be positive")
        self._client = client
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.claim_idle_ms = claim_idle_ms
        self._started = False
        self._claim_cursor = "0-0"

    async def start(self) -> None:
        """Create the group if necessary, propagating all errors except BUSYGROUP."""

        if self._started:
            return
        try:
            await _maybe_await(
                self._client.xgroup_create(
                    self.stream_name,
                    self.group_name,
                    id="0-0",
                    mkstream=True,
                )
            )
        except Exception as error:
            if not _is_busy_group(error):
                raise
        self._started = True

    async def ensure_group(self) -> None:
        """Explicit startup alias used by process supervisors."""

        await self.start()

    async def read_new(self) -> list[RedisInferenceMessage]:
        """Read up to ``batch_size`` new entries using ``XREADGROUP``."""

        await self.start()
        response = await _maybe_await(
            self._client.xreadgroup(
                groupname=self.group_name,
                consumername=self.consumer_name,
                streams={self.stream_name: ">"},
                count=self.batch_size,
                block=self.block_ms,
            )
        )
        return _messages_from_read_response(response, default_stream=self.stream_name)

    async def claim_stale(self) -> list[RedisInferenceMessage]:
        """Transfer stale Pending entries to this process with ``XAUTOCLAIM``."""

        await self.start()
        response = await _maybe_await(
            self._client.xautoclaim(
                name=self.stream_name,
                groupname=self.group_name,
                consumername=self.consumer_name,
                min_idle_time=self.claim_idle_ms,
                start_id=self._claim_cursor,
                count=self.batch_size,
            )
        )
        next_cursor, entries = _autoclaim_parts(response)
        self._claim_cursor = next_cursor
        if next_cursor == "0-0":
            # Redis signals a complete scan with 0-0.  Starting over on the
            # next recovery tick lets newly-idle entries be found without
            # retaining a stale cursor forever.
            self._claim_cursor = "0-0"
        return _messages_from_entries(entries, default_stream=self.stream_name)

    async def ack(self, message_id: str) -> int:
        """ACK one entry after its durable database outcome has committed."""

        await self.start()
        result = await _maybe_await(
            self._client.xack(self.stream_name, self.group_name, message_id)
        )
        if isinstance(result, bool):
            return int(result)
        if isinstance(result, int):
            return result
        try:
            return int(result)
        except (TypeError, ValueError) as error:
            raise TypeError("Redis XACK returned a non-integer response") from error

    async def close(self) -> None:
        """Close an injected Redis client when it exposes an async close hook."""

        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            await _maybe_await(close())


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _messages_from_read_response(
    response: object, *, default_stream: str
) -> list[RedisInferenceMessage]:
    if response is None:
        return []
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes, bytearray)):
        raise TypeError("Redis XREADGROUP response must be a sequence")
    messages: list[RedisInferenceMessage] = []
    for stream_result in response:
        if not isinstance(stream_result, Sequence) or len(stream_result) != 2:
            raise ValueError("Redis XREADGROUP stream result must contain stream and entries")
        stream_name, entries = stream_result
        messages.extend(
            _messages_from_entries(entries, default_stream=_decode(stream_name) or default_stream)
        )
    return messages


def _autoclaim_parts(response: object) -> tuple[str, object]:
    if response is None:
        return "0-0", []
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes, bytearray)):
        raise TypeError("Redis XAUTOCLAIM response must be a sequence")
    if len(response) < 2:
        raise ValueError("Redis XAUTOCLAIM response must contain cursor and entries")
    return _decode(response[0]), response[1]


def _messages_from_entries(
    entries: object, *, default_stream: str
) -> list[RedisInferenceMessage]:
    if entries is None:
        return []
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
        raise TypeError("Redis Stream entries must be a sequence")
    result: list[RedisInferenceMessage] = []
    for entry in entries:
        if not isinstance(entry, Sequence) or len(entry) != 2:
            raise ValueError("Redis Stream entry must contain id and fields")
        message_id, raw_fields = entry
        try:
            fields = _fields_from_response(raw_fields)
        except (TypeError, ValueError, UnicodeDecodeError):
            # A Stream entry with a valid id is individually recoverable: keep
            # its malformed fields as a bounded envelope so the Worker can
            # durably quarantine and ACK that poison delivery.  Transport or
            # database implementation errors are not caught here.
            fields = {"envelope": _bounded_malformed_fields(raw_fields)}
        result.append(
            RedisInferenceMessage(
                message_id=_decode(message_id),
                fields=fields,
                stream=default_stream,
            )
        )
    return result


def _fields_from_response(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return {_decode(key): item for key, item in value.items()}
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("Redis Stream fields must be a mapping or alternating sequence")
    if len(value) % 2:
        raise ValueError("Redis Stream fields must contain key/value pairs")
    return {
        _decode(value[index]): value[index + 1]
        for index in range(0, len(value), 2)
    }


def _bounded_malformed_fields(value: object) -> bytes:
    """Retain bounded poison-message evidence without trusting its shape."""

    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8", errors="surrogatepass")
    else:
        payload = repr(value).encode("utf-8", errors="replace")
    return payload[:MAX_ENVELOPE_BYTES]


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    return str(value)


def _is_busy_group(error: BaseException) -> bool:
    """Recognize Redis's group-exists response without hiding other failures."""

    return "BUSYGROUP" in str(error).upper()


__all__ = [
    "CLAIM_IDLE_MS",
    "GROUP_NAME",
    "MAX_ENVELOPE_BYTES",
    "READ_BLOCK_MS",
    "READ_COUNT",
    "STREAM_NAME",
    "InferenceMessage",
    "RedisInferenceClient",
    "RedisInferenceConsumer",
    "RedisInferenceMessage",
]
