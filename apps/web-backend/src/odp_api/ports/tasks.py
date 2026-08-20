"""Ports for durable asynchronous work and its operational signals."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from odp_api.modules.tasks.models import TaskDeadLetterAlert, TaskRecord


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

    def active(self) -> Sequence[TaskRecord]: ...
