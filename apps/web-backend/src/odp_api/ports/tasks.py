"""Ports for durable asynchronous work and its operational signals."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from odp_api.modules.tasks.commands import LeaseClaim, PublishInferenceCommand
from odp_api.modules.tasks.models import TaskDeadLetterAlert, TaskRecord


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """Tenant-scoped frame metadata needed before an object upload."""

    organization_id: UUID
    camera_id: UUID
    stream_session_id: UUID
    frame_sequence: int
    captured_at: datetime
    content_sha256: str
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class AdmissionReservation:
    """A database reservation that permits one frame object upload."""

    reservation_id: UUID
    artifact_id: UUID
    organization_id: UUID
    camera_id: UUID
    stream_session_id: UUID
    frame_sequence: int
    captured_at: datetime
    content_sha256: str
    evicted_task_id: UUID | None = None


class AdmissionRejected(RuntimeError):
    """Raised when a frame cannot be admitted before object storage upload."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class CameraAdmissionPort(Protocol):
    """Reserves camera capacity before uploading a frame object."""

    def reserve(self, request: AdmissionRequest, now: datetime) -> AdmissionReservation: ...

    def complete_upload(
        self,
        reservation_id: UUID,
        organization_id: UUID,
        object_key: str,
        content_length: int,
        now: datetime,
    ) -> TaskRecord: ...

    def fail_upload(
        self, reservation_id: UUID, organization_id: UUID, error_code: str, now: datetime
    ) -> None: ...


class TaskQueuePort(Protocol):
    """Publishes task records for a worker without coupling the service to Redis."""

    def enqueue(self, task: TaskRecord) -> None: ...

    def depth(self) -> int: ...


class TaskMetricsPort(Protocol):
    """Reports queue state needed by operational alerting."""

    def set_queue_depth(self, depth: int) -> None: ...

    def increment_dead_letter(self) -> None: ...


class TaskAlertPublisherPort(Protocol):
    """Writes an alert event when a task exhausts its retry budget."""

    def publish(self, alert: TaskDeadLetterAlert) -> None: ...


class TaskRepositoryPort(Protocol):
    """Persists idempotency keys and task state transitions."""

    def get(self, task_id: UUID) -> TaskRecord | None: ...

    def get_by_idempotency_key(self, idempotency_key: str) -> TaskRecord | None: ...

    def save(self, task: TaskRecord) -> TaskRecord: ...

    def outstanding(self) -> Sequence[TaskRecord]: ...

    def unpublished(self) -> Sequence[TaskRecord]: ...


class StaleLease(RuntimeError):
    """The worker no longer owns a live fencing lease."""


class TaskExecutionPort(Protocol):
    def claim(
        self, task_id: UUID, organization_id: UUID, worker_id: str, now: datetime
    ) -> LeaseClaim | None: ...
    def renew(self, claim: LeaseClaim, now: datetime) -> LeaseClaim | None: ...
    def complete_no_defect(self, command: PublishInferenceCommand) -> None: ...
    def record_failure(self, claim: LeaseClaim, failure: object, now: datetime) -> None: ...
