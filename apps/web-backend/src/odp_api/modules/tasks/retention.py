"""Typed Redis 7 retention planning that never crosses consumer-group PELs."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

INFERENCE_STREAM_NAME = "odp:inference:tasks"
ALERT_STREAM_NAME = "odp:inspection:alerts"
GATEWAY_GROUP_PREFIX = "odp-alert-gateway:"
_REDIS_ID_PATTERN = re.compile(r"(0|[1-9][0-9]*)-(0|[1-9][0-9]*)\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, order=True, slots=True)
class RedisStreamId:
    """A Redis Stream ID ordered by numeric millisecond/sequence components."""

    milliseconds: int
    sequence: int

    @classmethod
    def parse(cls, value: object) -> RedisStreamId | None:
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeDecodeError:
                return None
        if not isinstance(value, str):
            return None
        match = _REDIS_ID_PATTERN.fullmatch(value)
        if match is None:
            return None
        return cls(int(match.group(1)), int(match.group(2)))

    def __str__(self) -> str:
        return f"{self.milliseconds}-{self.sequence}"


@dataclass(frozen=True, slots=True)
class GroupProgress:
    """The only group state allowed to influence a trim watermark."""

    group_name: str
    last_delivered_id: str | None
    smallest_pending_id: str | None
    trusted: bool = True


class SafeTrimPlanner:
    """Compute a conservative exact-MINID threshold across every group."""

    @staticmethod
    def safe_min_id(
        groups: Sequence[GroupProgress], retention_floor_id: str
    ) -> str | None:
        floor = RedisStreamId.parse(retention_floor_id)
        if floor is None or not groups:
            return None

        progress_ids: list[RedisStreamId] = []
        for group in groups:
            if (
                not isinstance(group, GroupProgress)
                or not group.trusted
                or not isinstance(group.group_name, str)
                or not group.group_name.strip()
            ):
                return None
            delivered = RedisStreamId.parse(group.last_delivered_id)
            if delivered is None:
                return None
            if group.smallest_pending_id is None:
                progress = delivered
            else:
                pending = RedisStreamId.parse(group.smallest_pending_id)
                if pending is None:
                    return None
                progress = pending
            progress_ids.append(progress)

        return str(min(floor, *progress_ids))


@dataclass(frozen=True, slots=True)
class MinimumRetentionPolicy:
    """Derive an exact Redis ID from a positive UTC wall-clock duration."""

    duration: timedelta

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0):
            raise ValueError("minimum retention duration must be positive")

    def floor_id(self, now: datetime) -> str:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("retention clock must return a timezone-aware datetime")
        floor = now.astimezone(UTC) - self.duration
        milliseconds = (floor - _EPOCH) // timedelta(milliseconds=1)
        if milliseconds < 0:
            raise ValueError("retention floor cannot predate the Redis epoch")
        return f"{milliseconds}-0"


@dataclass(frozen=True, slots=True)
class GatewayGroupLease:
    """Explicit expiry authority for one temporary Realtime Gateway group."""

    group_name: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.group_name.startswith(GATEWAY_GROUP_PREFIX) or not self.group_name[
            len(GATEWAY_GROUP_PREFIX) :
        ].strip():
            raise ValueError("Gateway group is outside the explicit allowlist")
        if "\x00" in self.group_name:
            raise ValueError("Gateway group is outside the explicit allowlist")
        if self.expires_at.tzinfo is None:
            raise ValueError("Gateway group expiry must be timezone-aware")


class GatewayGroupExpiryRegistry:
    """Typed registry; Redis group existence is never treated as expiry authority."""

    def __init__(self, leases: Sequence[GatewayGroupLease]) -> None:
        normalized = tuple(leases)
        if any(not isinstance(lease, GatewayGroupLease) for lease in normalized):
            raise TypeError("Gateway group registry requires GatewayGroupLease values")
        names = tuple(lease.group_name for lease in normalized)
        if len(names) != len(set(names)):
            raise ValueError("Gateway group registry contains duplicate groups")
        self._leases = normalized

    def expired_groups(self, now: datetime) -> tuple[str, ...]:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Gateway expiry clock must be timezone-aware")
        current = now.astimezone(UTC)
        return tuple(
            lease.group_name
            for lease in self._leases
            if lease.expires_at.astimezone(UTC) <= current
        )


@dataclass(frozen=True, slots=True)
class StreamRetentionPolicy:
    """One explicit stream and its independently configurable safety policy."""

    stream_name: str
    minimum_retention: MinimumRetentionPolicy
    gateway_groups: GatewayGroupExpiryRegistry | None = None

    def __post_init__(self) -> None:
        if not self.stream_name.strip():
            raise ValueError("stream_name is required")
        if not isinstance(self.minimum_retention, MinimumRetentionPolicy):
            raise TypeError("minimum_retention must be a MinimumRetentionPolicy")
        if self.gateway_groups is not None and self.stream_name != ALERT_STREAM_NAME:
            raise ValueError("Gateway group cleanup is allowed only for the alert stream")


@dataclass(frozen=True, slots=True)
class TrimSummary:
    stream_name: str
    safe_min_id: str | None
    groups_considered: int
    trimmed_entries: int
    destroyed_groups: tuple[str, ...] = ()


class RedisRetentionClient(Protocol):
    """Exact async Redis command surface used by the retention adapter."""

    def xinfo_groups(self, stream: str) -> object | Awaitable[object]: ...
    def xpending(self, stream: str, group: str) -> object | Awaitable[object]: ...
    def xgroup_destroy(self, stream: str, group: str) -> object | Awaitable[object]: ...
    def execute_command(self, *args: str) -> object | Awaitable[object]: ...


class RedisRetentionAdapter:
    """Normalize Redis 7 group state and expose only exact MINID trimming."""

    def __init__(self, client: RedisRetentionClient) -> None:
        self._client = client

    async def group_progress(self, stream_name: str) -> tuple[GroupProgress, ...]:
        response = await _maybe_await(self._client.xinfo_groups(stream_name))
        if not _is_sequence(response):
            return (GroupProgress("", None, None, trusted=False),)

        progress: list[GroupProgress] = []
        for raw_group in response:
            if not isinstance(raw_group, Mapping):
                progress.append(GroupProgress("", None, None, trusted=False))
                continue
            group_name = _decode_text(_mapping_value(raw_group, "name"))
            delivered = _decode_text(_mapping_value(raw_group, "last-delivered-id"))
            if group_name is None or not group_name.strip():
                progress.append(GroupProgress("", delivered, None, trusted=False))
                continue
            pending = await _maybe_await(self._client.xpending(stream_name, group_name))
            smallest, pending_trusted = _smallest_pending(pending)
            progress.append(
                GroupProgress(
                    group_name,
                    delivered,
                    smallest,
                    trusted=pending_trusted,
                )
            )
        return tuple(progress)

    async def destroy_group(self, stream_name: str, group_name: str) -> bool:
        if stream_name != ALERT_STREAM_NAME:
            raise ValueError("Gateway group cleanup is allowed only for the alert stream")
        if (
            not group_name.startswith(GATEWAY_GROUP_PREFIX)
            or not group_name[len(GATEWAY_GROUP_PREFIX) :].strip()
            or "\x00" in group_name
        ):
            raise ValueError("refusing to destroy a group outside the Gateway allowlist")
        result = await _maybe_await(self._client.xgroup_destroy(stream_name, group_name))
        return _integer_result(result, command="XGROUP DESTROY") > 0

    async def trim_min_id(self, stream_name: str, safe_min_id: str) -> int:
        if RedisStreamId.parse(safe_min_id) is None:
            raise ValueError("safe_min_id is not a numeric Redis Stream ID")
        result = await _maybe_await(
            self._client.execute_command(
                "XTRIM", stream_name, "MINID", "=", safe_min_id
            )
        )
        return _integer_result(result, command="XTRIM")


class StreamRetentionController:
    """Apply one explicit policy without consuming or claiming Stream entries."""

    def __init__(
        self,
        adapter: RedisRetentionAdapter,
        policy: StreamRetentionPolicy,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(adapter, RedisRetentionAdapter):
            raise TypeError("adapter must be a RedisRetentionAdapter")
        if not isinstance(policy, StreamRetentionPolicy):
            raise TypeError("policy must be a StreamRetentionPolicy")
        self._adapter = adapter
        self.policy = policy
        self._clock = clock

    async def run_once(self) -> TrimSummary:
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("retention clock must return datetime")

        destroyed: list[str] = []
        if self.policy.gateway_groups is not None:
            for group_name in self.policy.gateway_groups.expired_groups(now):
                if await self._adapter.destroy_group(self.policy.stream_name, group_name):
                    destroyed.append(group_name)

        groups = await self._adapter.group_progress(self.policy.stream_name)
        safe_min_id = SafeTrimPlanner.safe_min_id(
            groups,
            self.policy.minimum_retention.floor_id(now),
        )
        trimmed = (
            await self._adapter.trim_min_id(self.policy.stream_name, safe_min_id)
            if safe_min_id is not None
            else 0
        )
        return TrimSummary(
            stream_name=self.policy.stream_name,
            safe_min_id=safe_min_id,
            groups_considered=len(groups),
            trimmed_entries=trimmed,
            destroyed_groups=tuple(destroyed),
        )


async def _maybe_await(value: object | Awaitable[object]) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _mapping_value(mapping: Mapping[object, object], name: str) -> object | None:
    if name in mapping:
        return mapping[name]
    encoded = name.encode("ascii")
    return mapping.get(encoded)


def _smallest_pending(response: object) -> tuple[str | None, bool]:
    if not isinstance(response, Mapping):
        return None, False
    count = _mapping_value(response, "pending")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None, False
    minimum = _mapping_value(response, "min")
    maximum = _mapping_value(response, "max")
    consumers = _mapping_value(response, "consumers")
    if count == 0:
        return (
            None,
            minimum is None
            and maximum is None
            and _is_sequence(consumers)
            and len(consumers) == 0,
        )
    decoded_minimum = _decode_text(minimum)
    decoded_maximum = _decode_text(maximum)
    parsed_minimum = RedisStreamId.parse(decoded_minimum)
    parsed_maximum = RedisStreamId.parse(decoded_maximum)
    trusted_consumers = _pending_consumers_total(consumers)
    trusted = (
        parsed_minimum is not None
        and parsed_maximum is not None
        and parsed_minimum <= parsed_maximum
        and trusted_consumers == count
    )
    return decoded_minimum, trusted


def _pending_consumers_total(value: object) -> int | None:
    if not _is_sequence(value) or not value:
        return None
    total = 0
    for consumer in value:
        if not isinstance(consumer, Mapping):
            return None
        name = _decode_text(_mapping_value(consumer, "name"))
        pending = _mapping_value(consumer, "pending")
        if (
            name is None
            or not name.strip()
            or not isinstance(pending, int)
            or isinstance(pending, bool)
            or pending < 1
        ):
            return None
        total += pending
    return total


def _decode_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _integer_result(value: object, *, command: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise TypeError(f"Redis {command} returned a non-integer response") from error


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


__all__ = [
    "ALERT_STREAM_NAME",
    "GATEWAY_GROUP_PREFIX",
    "INFERENCE_STREAM_NAME",
    "GatewayGroupExpiryRegistry",
    "GatewayGroupLease",
    "GroupProgress",
    "MinimumRetentionPolicy",
    "RedisRetentionAdapter",
    "RedisStreamId",
    "SafeTrimPlanner",
    "StreamRetentionController",
    "StreamRetentionPolicy",
    "TrimSummary",
]
