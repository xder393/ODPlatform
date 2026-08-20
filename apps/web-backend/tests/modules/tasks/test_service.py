import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC)]

from odp_api.adapters.tasks.redis_stream import RedisStreamTaskQueue
from odp_api.adapters.tasks.sqlite import SQLiteTaskRepository
from odp_api.modules.tasks.service import (
    InMemoryTaskAlertPublisher,
    InMemoryTaskMetrics,
    InMemoryTaskQueue,
    InMemoryTaskRepository,
    TaskService,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def dependencies() -> tuple[
    InMemoryTaskRepository,
    InMemoryTaskQueue,
    InMemoryTaskMetrics,
    InMemoryTaskAlertPublisher,
]:
    return (
        InMemoryTaskRepository(),
        InMemoryTaskQueue(),
        InMemoryTaskMetrics(),
        InMemoryTaskAlertPublisher(),
    )


def make_service(
    dependencies: tuple[
        InMemoryTaskRepository,
        InMemoryTaskQueue,
        InMemoryTaskMetrics,
        InMemoryTaskAlertPublisher,
    ]
) -> TaskService:
    repository, queue, metrics, alerts = dependencies
    return TaskService(repository, queue, metrics, alerts)


def test_duplicate_frame_model_enqueue_reuses_the_existing_task(
    dependencies: tuple[
        InMemoryTaskRepository,
        InMemoryTaskQueue,
        InMemoryTaskMetrics,
        InMemoryTaskAlertPublisher,
    ],
) -> None:
    """A broken idempotency check would enqueue the same frame/model twice."""
    service = make_service(dependencies)

    first = service.enqueue(
        "vision_inference",
        "frame-001:model-v3",
        {"frame_id": "frame-001", "model_version": "model-v3"},
    )
    duplicate = service.enqueue(
        "vision_inference",
        "frame-001:model-v3",
        {"frame_id": "frame-001", "model_version": "model-v3"},
    )

    assert duplicate.task_id == first.task_id
    assert dependencies[1].queued_task_ids == [first.task_id]
    assert dependencies[2].task_queue_depth == 1


def test_vision_timeout_retries_with_exponential_delays(
    dependencies: tuple[
        InMemoryTaskRepository,
        InMemoryTaskQueue,
        InMemoryTaskMetrics,
        InMemoryTaskAlertPublisher,
    ],
    now: datetime,
) -> None:
    """Removing the timeout retry branch would leave a timed-out frame running."""
    service = make_service(dependencies)
    task = service.enqueue("vision_inference", "frame-002:model-v3", {"frame_id": "frame-002"})

    async def exceeds_vision_timeout(_: dict[str, object]) -> None:
        raise asyncio.TimeoutError

    first_retry = asyncio.run(service.process_vision(task.task_id, exceeds_vision_timeout, now))
    second_retry = asyncio.run(
        service.process_vision(
            task.task_id,
            exceeds_vision_timeout,
            now + timedelta(seconds=1),
        )
    )

    assert first_retry.status == "RETRYING"
    assert first_retry.attempt_count == 1
    assert first_retry.next_attempt_at == now + timedelta(seconds=1)
    assert second_retry.status == "RETRYING"
    assert second_retry.attempt_count == 2
    assert second_retry.next_attempt_at == now + timedelta(seconds=3)


def test_third_failed_vision_attempt_enters_dead_letter_and_emits_alert(
    dependencies: tuple[
        InMemoryTaskRepository,
        InMemoryTaskQueue,
        InMemoryTaskMetrics,
        InMemoryTaskAlertPublisher,
    ],
    now: datetime,
) -> None:
    """An exhausted task must not be retried forever or fail without an alert."""
    service = make_service(dependencies)
    task = service.enqueue("vision_inference", "frame-003:model-v3", {"frame_id": "frame-003"})

    async def inference_fails(_: dict[str, object]) -> None:
        raise RuntimeError("vision provider unavailable")

    asyncio.run(service.process_vision(task.task_id, inference_fails, now))
    asyncio.run(service.process_vision(task.task_id, inference_fails, now + timedelta(seconds=1)))
    exhausted = asyncio.run(service.process_vision(task.task_id, inference_fails, now + timedelta(seconds=3)))

    assert exhausted.status == "DEAD_LETTER"
    assert exhausted.attempt_count == 3
    assert dependencies[2].task_dead_letter_total == 1
    assert dependencies[3].events[0].task_id == task.task_id
    assert dependencies[3].events[0].error == "vision provider unavailable"


def test_stale_frame_backlog_is_skipped_without_expanding_task_statuses(
    dependencies: tuple[
        InMemoryTaskRepository,
        InMemoryTaskQueue,
        InMemoryTaskMetrics,
        InMemoryTaskAlertPublisher,
    ],
    now: datetime,
) -> None:
    """An overloaded queue must discard old frames rather than process obsolete work."""
    service = make_service(dependencies)
    stale = service.enqueue(
        "vision_inference",
        "frame-004:model-v3",
        {"frame_id": "frame-004", "enqueued_at": now - timedelta(seconds=3)},
    )
    fresh = service.enqueue(
        "vision_inference",
        "frame-005:model-v3",
        {"frame_id": "frame-005", "enqueued_at": now - timedelta(seconds=2)},
    )

    skipped = service.skip_stale_frames(now)

    assert [task.task_id for task in skipped] == [stale.task_id]
    assert skipped[0].frame_status == "SKIPPED"
    assert skipped[0].status == "FAILED"
    assert service.get(fresh.task_id).frame_status == "PENDING"


def test_redis_stream_adapter_publishes_a_serialized_task_reference(
    dependencies: tuple[
        InMemoryTaskRepository,
        InMemoryTaskQueue,
        InMemoryTaskMetrics,
        InMemoryTaskAlertPublisher,
    ],
) -> None:
    """Replacing the adapter with a local-only queue must break worker publication."""
    class FakeRedisStream:
        def __init__(self) -> None:
            self.entries: list[tuple[str, dict[str, str]]] = []

        def xadd(self, stream: str, fields: dict[str, str]) -> str:
            self.entries.append((stream, fields))
            return "1-0"

        def xlen(self, stream: str) -> int:
            return len([entry for entry in self.entries if entry[0] == stream])

    task = make_service(dependencies).enqueue(
        "vision_inference",
        "frame-006:model-v3",
        {"frame_id": "frame-006", "model_version": "model-v3"},
    )
    redis = FakeRedisStream()

    queue = RedisStreamTaskQueue(redis)
    queue.enqueue(task)

    assert redis.entries == [
        (
            "odp:tasks",
            {
                "task_id": str(task.task_id),
                "task_type": "vision_inference",
                "idempotency_key": "frame-006:model-v3",
                "payload": '{"frame_id": "frame-006", "model_version": "model-v3"}',
            },
        )
    ]
    assert queue.depth() == 1


def test_sqlite_task_repository_retains_idempotency_records_after_restart(tmp_path: Path) -> None:
    """Replacing the runtime repository must not let a restart duplicate a frame/model task."""
    database_path = tmp_path / "tasks.sqlite3"
    first_repository = SQLiteTaskRepository(database_path)
    first_queue = InMemoryTaskQueue()
    first_service = TaskService(
        first_repository,
        first_queue,
        InMemoryTaskMetrics(),
        InMemoryTaskAlertPublisher(),
    )
    enqueued = first_service.enqueue(
        "vision_inference",
        "frame-007:model-v3",
        {"frame_id": "frame-007", "model_version": "model-v3"},
    )
    first_repository.close()

    restarted_repository = SQLiteTaskRepository(database_path)
    restarted_service = TaskService(
        restarted_repository,
        InMemoryTaskQueue(),
        InMemoryTaskMetrics(),
        InMemoryTaskAlertPublisher(),
    )
    duplicate = restarted_service.enqueue(
        "vision_inference",
        "frame-007:model-v3",
        {"frame_id": "frame-007", "model_version": "model-v3"},
    )

    assert duplicate.task_id == enqueued.task_id
    restarted_repository.close()


def test_sqlite_task_repository_uniquely_persists_concurrent_idempotency_keys(tmp_path: Path) -> None:
    """Two API processes must not race past the durable idempotency constraint."""
    database_path = tmp_path / "tasks.sqlite3"
    barrier = Barrier(2)

    class ConcurrentRepository(SQLiteTaskRepository):
        def get_by_idempotency_key(self, idempotency_key: str):
            record = super().get_by_idempotency_key(idempotency_key)
            barrier.wait()
            return record

    repositories = [ConcurrentRepository(database_path), ConcurrentRepository(database_path)]

    def enqueue(repository: SQLiteTaskRepository):
        return TaskService(
            repository,
            InMemoryTaskQueue(),
            InMemoryTaskMetrics(),
            InMemoryTaskAlertPublisher(),
        ).enqueue(
            "vision_inference",
            "frame-007b:model-v3",
            {"frame_id": "frame-007b", "model_version": "model-v3"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        records = list(executor.map(enqueue, repositories))

    assert records[0].task_id == records[1].task_id
    for repository in repositories:
        repository.close()


def test_vision_processing_invokes_the_30_second_timeout_mechanism(
    dependencies: tuple[
        InMemoryTaskRepository,
        InMemoryTaskQueue,
        InMemoryTaskMetrics,
        InMemoryTaskAlertPublisher,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the deadline from wait_for would allow an inference to run unbounded."""
    service = make_service(dependencies)
    task = service.enqueue("vision_inference", "frame-008:model-v3", {"frame_id": "frame-008"})
    timeouts: list[int] = []

    async def controlled_wait_for(awaitable, *, timeout: int):
        timeouts.append(timeout)
        return await awaitable

    async def inference(_: dict[str, object]) -> None:
        return None

    monkeypatch.setattr("odp_api.modules.tasks.service.asyncio.wait_for", controlled_wait_for)

    asyncio.run(service.process_vision(task.task_id, inference))

    assert timeouts == [30]


def test_retry_queue_depth_metric_uses_the_queue_transport(
    dependencies: tuple[
        InMemoryTaskRepository,
        InMemoryTaskQueue,
        InMemoryTaskMetrics,
        InMemoryTaskAlertPublisher,
    ],
    now: datetime,
) -> None:
    """Counting active records hides the additional queued retry message from operations."""
    service = make_service(dependencies)
    task = service.enqueue("vision_inference", "frame-009:model-v3", {"frame_id": "frame-009"})

    async def inference_fails(_: dict[str, object]) -> None:
        raise RuntimeError("transient vision failure")

    asyncio.run(service.process_vision(task.task_id, inference_fails, now))

    assert dependencies[1].depth() == 2
    assert dependencies[2].task_queue_depth == 2
