"""Idempotent, bounded processing for asynchronous application work."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from odp_api.modules.tasks.models import TaskDeadLetterAlert, TaskRecord
from odp_api.ports.tasks import (
    TaskAlertPublisherPort,
    TaskMetricsPort,
    TaskQueuePort,
    TaskRepositoryPort,
)

MAX_ATTEMPTS = 3
VISION_TIMEOUT_SECONDS = 30
STALE_FRAME_CUTOFF_SECONDS = 2


class InMemoryTaskRepository:
    """A persistence-shaped repository used by local runtime and unit tests."""

    def __init__(self) -> None:
        self._records: dict[UUID, TaskRecord] = {}
        self._ids_by_key: dict[str, UUID] = {}

    def get(self, task_id: UUID) -> TaskRecord | None:
        return self._records.get(task_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> TaskRecord | None:
        task_id = self._ids_by_key.get(idempotency_key)
        return self._records.get(task_id) if task_id is not None else None

    def save(self, task: TaskRecord) -> TaskRecord:
        existing_id = self._ids_by_key.get(task.idempotency_key)
        if existing_id is not None and existing_id != task.task_id:
            return self._records[existing_id]
        self._records[task.task_id] = task
        self._ids_by_key[task.idempotency_key] = task.task_id
        return task

    def outstanding(self) -> Sequence[TaskRecord]:
        return tuple(
            record
            for record in self._records.values()
            if record.status in {"PENDING", "RETRYING"}
        )

    def unpublished(self) -> Sequence[TaskRecord]:
        return tuple(
            record for record in self.outstanding() if record.published_at is None
        )


class InMemoryTaskQueue:
    """Local queue transport retaining messages for deterministic tests."""

    def __init__(self) -> None:
        self.queued_task_ids: list[UUID] = []

    def enqueue(self, task: TaskRecord) -> None:
        self.queued_task_ids.append(task.task_id)

    def depth(self) -> int:
        return len(self.queued_task_ids)


class InMemoryTaskMetrics:
    """Minimal metric sink mirroring queue-depth and dead-letter counters."""

    def __init__(self) -> None:
        self.task_queue_depth = 0
        self.task_dead_letter_total = 0

    def set_queue_depth(self, depth: int) -> None:
        self.task_queue_depth = depth

    def increment_dead_letter(self) -> None:
        self.task_dead_letter_total += 1


class InMemoryTaskAlertPublisher:
    """Preserves final-failure alerts without requiring external notifications."""

    def __init__(self) -> None:
        self.events: list[TaskDeadLetterAlert] = []

    def publish(self, alert: TaskDeadLetterAlert) -> None:
        self.events.append(alert)


class TaskService:
    """Owns task idempotency, state transitions, retries, and frame shedding."""

    def __init__(
        self,
        repository: TaskRepositoryPort,
        queue: TaskQueuePort,
        metrics: TaskMetricsPort,
        alerts: TaskAlertPublisherPort,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._metrics = metrics
        self._alerts = alerts

    def enqueue(
        self, task_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> TaskRecord:
        existing = self._repository.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return self._publish_if_needed(existing)
        created_at = _payload_timestamp(payload.get("enqueued_at")) or datetime.now(UTC)
        record = TaskRecord(
            task_id=uuid4(),
            task_type=task_type,
            idempotency_key=idempotency_key,
            payload=dict(payload),
            status="PENDING",
            attempt_count=0,
            created_at=created_at,
            frame_status="PENDING" if task_type == "vision_inference" else None,
        )
        persisted = self._repository.save(record)
        return self._publish_if_needed(persisted)

    def recover_unpublished(self) -> list[TaskRecord]:
        """Republish persisted work left behind when a prior queue write failed."""
        return [
            self._publish_if_needed(task) for task in self._repository.unpublished()
        ]

    def get(self, task_id: UUID) -> TaskRecord:
        record = self._repository.get(task_id)
        if record is None:
            raise KeyError(f"Unknown task: {task_id}")
        return record

    async def process_vision(
        self,
        task_id: UUID,
        inference: Callable[[dict[str, object]], Awaitable[None]],
        now: datetime | None = None,
    ) -> TaskRecord:
        """Run one eligible frame with a hard 30-second inference deadline."""
        current_time = now or datetime.now(UTC)
        task = self.get(task_id)
        if task.status not in {"PENDING", "RETRYING"} or task.frame_status == "SKIPPED":
            return task
        if task.next_attempt_at is not None and current_time < task.next_attempt_at:
            return task

        running = self._save(replace(task, status="RUNNING", next_attempt_at=None))
        try:
            await asyncio.wait_for(
                inference(running.payload), timeout=VISION_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            return self._retry_or_dead_letter(
                running,
                "vision inference exceeded 30 second timeout",
                current_time,
            )
        except Exception as error:  # noqa: BLE001 - worker failures must be persisted as retries.
            return self._retry_or_dead_letter(running, str(error), current_time)
        return self._save(replace(running, status="SUCCEEDED", last_error=None))

    def skip_stale_frames(self, now: datetime | None = None) -> list[TaskRecord]:
        """Mark queued vision frames older than two seconds as skipped work."""
        current_time = now or datetime.now(UTC)
        skipped: list[TaskRecord] = []
        for task in self._repository.outstanding():
            if task.task_type != "vision_inference" or task.frame_status != "PENDING":
                continue
            if current_time - task.created_at <= timedelta(
                seconds=STALE_FRAME_CUTOFF_SECONDS
            ):
                continue
            skipped.append(
                self._save(
                    replace(
                        task,
                        status="FAILED",
                        frame_status="SKIPPED",
                        last_error="frame skipped because queue backlog exceeded 2 seconds",
                    )
                )
            )
        return skipped

    def _retry_or_dead_letter(
        self, task: TaskRecord, error: str, now: datetime
    ) -> TaskRecord:
        attempt_count = task.attempt_count + 1
        if attempt_count >= MAX_ATTEMPTS:
            dead_letter = self._save(
                replace(
                    task,
                    status="DEAD_LETTER",
                    attempt_count=attempt_count,
                    last_error=error,
                )
            )
            self._metrics.increment_dead_letter()
            self._alerts.publish(
                TaskDeadLetterAlert(
                    task_id=dead_letter.task_id,
                    task_type=dead_letter.task_type,
                    idempotency_key=dead_letter.idempotency_key,
                    error=error,
                    occurred_at=now,
                )
            )
            return dead_letter

        retry = self._save(
            replace(
                task,
                status="RETRYING",
                attempt_count=attempt_count,
                last_error=error,
                next_attempt_at=now + timedelta(seconds=2 ** (attempt_count - 1)),
                published_at=None,
            )
        )
        return self._publish_if_needed(retry)

    def _publish_if_needed(self, task: TaskRecord) -> TaskRecord:
        if task.status not in {"PENDING", "RETRYING"} or task.published_at is not None:
            self._update_queue_depth()
            return task
        try:
            self._queue.enqueue(task)
        except OSError as error:
            return self._save(
                replace(task, last_error=f"queue publication pending: {error}")
            )
        return self._save(replace(task, published_at=datetime.now(UTC)))

    def _save(self, task: TaskRecord) -> TaskRecord:
        saved = self._repository.save(task)
        self._update_queue_depth()
        return saved

    def _update_queue_depth(self) -> None:
        self._metrics.set_queue_depth(len(self._repository.outstanding()))


def _payload_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None
