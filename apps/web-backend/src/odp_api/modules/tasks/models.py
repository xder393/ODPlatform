"""Task state records kept independently of a particular queue implementation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

TaskStatus = Literal[
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "RETRYING", "DEAD_LETTER"
]
FrameStatus = Literal["PENDING", "SKIPPED"]


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: UUID
    task_type: str
    idempotency_key: str
    payload: dict[str, object]
    status: TaskStatus
    attempt_count: int
    created_at: datetime
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    frame_status: FrameStatus | None = None
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TaskDeadLetterAlert:
    task_id: UUID
    task_type: str
    idempotency_key: str
    error: str
    occurred_at: datetime
