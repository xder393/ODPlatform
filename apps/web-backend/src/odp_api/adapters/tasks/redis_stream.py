"""Redis Stream queue adapter.

The adapter depends only on the small Redis command surface it needs, so unit
tests can supply a deterministic fake and do not need a live Redis server.
"""

import json
from typing import Protocol

from odp_api.modules.tasks.models import TaskRecord


class RedisStreamClient(Protocol):
    def xadd(self, stream: str, fields: dict[str, str]) -> object: ...

    def xlen(self, stream: str) -> int: ...


class RedisStreamTaskQueue:
    """Publishes task references to a Redis Stream for independently running workers."""

    def __init__(self, client: RedisStreamClient, stream_name: str = "odp:tasks") -> None:
        self._client = client
        self._stream_name = stream_name

    def enqueue(self, task: TaskRecord) -> None:
        self._client.xadd(
            self._stream_name,
            {
                "task_id": str(task.task_id),
                "task_type": task.task_type,
                "idempotency_key": task.idempotency_key,
                "payload": json.dumps(task.payload, default=_json_default, sort_keys=True),
            },
        )

    def depth(self) -> int:
        return self._client.xlen(self._stream_name)


def _json_default(value: object) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[union-attr]
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
