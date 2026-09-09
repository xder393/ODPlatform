"""Task state records and durable P1 control-plane vocabulary.

The P0 task adapters still use their original string states while the runtime
adapters are migrated in P1B.  The P1 enums therefore deliberately live next
to (rather than inside) the compatibility record below.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID


class TaskStatus(StrEnum):
    """Persisted states for a PostgreSQL-owned inference task."""

    READY = "READY"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    DEAD_LETTER = "DEAD_LETTER"
    BLOCKED_COMPATIBILITY = "BLOCKED_COMPATIBILITY"
    SKIPPED_STALE = "SKIPPED_STALE"
    SKIPPED_BACKPRESSURE = "SKIPPED_BACKPRESSURE"


class ArtifactState(StrEnum):
    """Storage/availability state of an uploaded frame artifact."""

    PENDING = "PENDING"
    AVAILABLE = "AVAILABLE"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"
    FAILED = "FAILED"


class ArtifactLifecycle(StrEnum):
    """Retention lifecycle for a frame artifact."""

    PROCESSING = "PROCESSING"
    EVIDENCE = "EVIDENCE"


class FailureKind(StrEnum):
    """Failure classification used by retry and quarantine policy."""

    RETRYABLE_INFRA = "RETRYABLE_INFRA"
    INVALID_INPUT = "INVALID_INPUT"
    MODEL_CONFIGURATION = "MODEL_CONFIGURATION"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"


FrameStatus = Literal["PENDING", "SKIPPED"]


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: UUID
    task_type: str
    idempotency_key: str
    payload: dict[str, object]
    # ``str`` retains compatibility with P0's PENDING/RETRYING adapter until
    # P1B replaces that adapter with the PostgreSQL control-plane repository.
    status: TaskStatus | str
    attempt_count: int
    created_at: datetime
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    frame_status: FrameStatus | None = None
    published_at: datetime | None = None

    # P1 fields are optional so existing SQLite/Redis P0 callers can continue
    # constructing this record during the adapter migration window.
    organization_id: UUID | None = None
    camera_id: UUID | None = None
    artifact_id: UUID | None = None
    dispatch_seq: int | None = None
    last_dispatched_at: datetime | None = None
    lease_owner: str | None = None
    fence_token: int | None = None
    lease_expires_at: datetime | None = None
    error_code: str | None = None
    error_detail: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TaskDeadLetterAlert:
    task_id: UUID
    task_type: str
    idempotency_key: str
    error: str
    occurred_at: datetime
