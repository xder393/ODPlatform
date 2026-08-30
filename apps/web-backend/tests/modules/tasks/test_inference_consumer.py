"""Behavioral tests for Redis inference delivery and fenced Worker execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

BACKEND_SRC = Path(__file__).parents[3] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[5] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(SHARED_SCHEMAS_SRC), str(BACKEND_SRC)]

from odp_schemas.events import EventEnvelope

from odp_api.modules.tasks.commands import (
    InferenceExecutionContract,
    LeaseClaim,
)
from odp_api.modules.tasks.consumer import (
    GROUP_NAME,
    STREAM_NAME,
    RedisInferenceConsumer,
)
from odp_api.modules.tasks.models import FailureKind, TaskRecord, TaskStatus
from odp_api.processes.inference_worker import InferenceResult, InferenceWorker

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _envelope(
    task_id: UUID,
    organization_id: UUID,
    *,
    schema_version: int = 1,
    event_type: str = "vision.inference.requested.v1",
    dispatch_seq: int = 1,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        schema_version=schema_version,
        occurred_at=NOW,
        correlation_id=uuid4(),
        organization_id=organization_id,
        aggregate_id=task_id,
        payload={"task_id": str(task_id), "dispatch_seq": dispatch_seq},
    )


def _message(envelope: EventEnvelope, message_id: str = "1-0") -> tuple[str, dict[str, str]]:
    return message_id, {"envelope": envelope.canonical_json()}


def _task(task_id: UUID, organization_id: UUID, *, status=TaskStatus.READY, dispatch_seq=1):
    return TaskRecord(
        task_id=task_id,
        task_type="vision_inference",
        idempotency_key=str(task_id),
        payload={"artifact_id": str(uuid4())},
        status=status,
        attempt_count=0,
        created_at=NOW,
        organization_id=organization_id,
        artifact_id=uuid4(),
        dispatch_seq=dispatch_seq,
    )


def _claim(task: TaskRecord, worker_id: str = "worker-a") -> LeaseClaim:
    return LeaseClaim(
        task_id=task.task_id,
        organization_id=task.organization_id,
        artifact_id=task.artifact_id,
        attempt_id=uuid4(),
        attempt_no=1,
        fence_token=1,
        lease_owner=worker_id,
        lease_expires_at=NOW,
    )


CONTRACT = InferenceExecutionContract(
    model_release="mock-1",
    model_sha256="b" * 64,
    onnxruntime_version="test",
    execution_provider="cpu",
    actual_input_shape=(1, 3, 32, 32),
    preprocessing_version="pre-v1",
    postprocessing_version="post-v1",
    confidence_threshold=0.5,
    iou_threshold=0.5,
    nms_mode="hard",
    nms_in_model=False,
    class_map_version="classes-v1",
)


class RecordingRedis:
    def __init__(self, read_result=None, claim_result=None):
        self.group_calls = []
        self.read_calls = []
        self.claim_calls = []
        self.ack_calls = []
        self.read_result = read_result
        self.claim_result = claim_result

    async def xgroup_create(self, *args, **kwargs):
        self.group_calls.append((args, kwargs))

    async def xreadgroup(self, **kwargs):
        self.read_calls.append(kwargs)
        return self.read_result

    async def xautoclaim(self, **kwargs):
        self.claim_calls.append(kwargs)
        return self.claim_result

    async def xack(self, *args):
        self.ack_calls.append(args)
        return 1


class BusyGroupRedis(RecordingRedis):
    async def xgroup_create(self, *args, **kwargs):
        raise RuntimeError("BUSYGROUP Consumer Group name already exists")


class FakeTaskRepository:
    def __init__(self, task):
        self.task = task

    async def get_task(self, task_id, organization_id):
        if task_id == self.task.task_id and organization_id == self.task.organization_id:
            return self.task
        return None


class FakeExecution:
    def __init__(self, claim_result):
        self.claim_result = claim_result
        self.claim_calls = []
        self.renew_calls = []
        self.failure_calls = []

    async def claim(self, *args):
        self.claim_calls.append(args)
        return self.claim_result

    async def renew(self, *args):
        self.renew_calls.append(args)
        return args[0]

    async def record_failure(self, *args):
        self.failure_calls.append(args)


class FakeArtifactLoader:
    async def load(self, task):
        return {"content": b"frame", "sha256": hashlib.sha256(b"frame").hexdigest()}


class FakeInference:
    async def infer(self, artifact, task):
        return InferenceResult(
            execution_contract=CONTRACT,
            frame_sha256=artifact.sha256,
            detections=(),
            stage_durations=(("model", 0.1),),
        )


class BlockingEffects:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.published = []

    async def publish(self, command):
        self.started.set()
        await self.release.wait()
        self.published.append(command)


class RecordingEffects:
    def __init__(self):
        self.published = []

    async def publish(self, command):
        self.published.append(command)


class RecordingAckConsumer:
    def __init__(self):
        self.acked = []

    async def ack(self, message_id):
        self.acked.append(message_id)


def _worker(task, *, effects, execution=None, consumer=None, loader=None, inference=None):
    return InferenceWorker(
        consumer or RecordingAckConsumer(),
        execution or FakeExecution(_claim(task)),
        effects,
        task_repository=FakeTaskRepository(task),
        artifact_loader=loader or FakeArtifactLoader(),
        inference=inference or FakeInference(),
        clock=lambda: NOW,
        worker_id="worker-a",
        renewal_interval_seconds=0.01,
        inference_timeout_seconds=0.1,
    )


async def test_worker_acks_only_after_effect_commit():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    consumer = RecordingAckConsumer()
    effects = BlockingEffects()
    worker = _worker(task, effects=effects, consumer=consumer)
    running = asyncio.create_task(worker.process(_message(_envelope(task_id, organization_id))))

    await effects.started.wait()
    assert consumer.acked == []
    effects.release.set()
    await running

    assert consumer.acked == ["1-0"]
    assert len(effects.published) == 1


async def test_terminal_duplicate_is_acked_without_new_attempt():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id, status=TaskStatus.SUCCEEDED)
    execution = FakeExecution(None)
    consumer = RecordingAckConsumer()
    effects = RecordingEffects()
    worker = _worker(task, effects=effects, execution=execution, consumer=consumer)

    await worker.process(_message(_envelope(task_id, organization_id)))

    assert execution.claim_calls == []
    assert effects.published == []
    assert consumer.acked == ["1-0"]


async def test_live_foreign_lease_stays_pending_when_claim_is_rejected():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    execution = FakeExecution(None)
    consumer = RecordingAckConsumer()
    effects = RecordingEffects()
    worker = _worker(task, effects=effects, execution=execution, consumer=consumer)

    await worker.process(_message(_envelope(task_id, organization_id)))

    assert consumer.acked == []
    assert effects.published == []


async def test_unsupported_schema_is_quarantined_before_ack_with_bounded_payload():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    execution = FakeExecution(None)
    consumer = RecordingAckConsumer()
    effects = RecordingEffects()

    class QuarantineExecution(FakeExecution):
        def __init__(self):
            super().__init__(None)
            self.quarantines = []

        async def quarantine_message(self, *args):
            self.quarantines.append(args)
            return type("QuarantineResult", (), {"ack_after_commit": True})()

    execution = QuarantineExecution()
    worker = _worker(task, effects=effects, execution=execution, consumer=consumer)
    envelope = _envelope(task_id, organization_id, schema_version=9)
    raw = json.dumps(
        {
            **json.loads(envelope.canonical_json()),
            "padding": "x" * 100_000,
        }
    ).encode()

    await worker.process(("2-0", {"envelope": raw}))

    assert len(execution.quarantines) == 1
    assert len(execution.quarantines[0][5]) <= 64 * 1024
    assert consumer.acked == ["2-0"]


async def test_timeout_is_recorded_as_retryable_failure_before_ack():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    claim = _claim(task)
    execution = FakeExecution(claim)

    class SlowInference:
        async def infer(self, artifact, task):
            await asyncio.Event().wait()

    consumer = RecordingAckConsumer()
    worker = _worker(
        task,
        effects=RecordingEffects(),
        execution=execution,
        consumer=consumer,
        inference=SlowInference(),
    )

    await worker.process(_message(_envelope(task_id, organization_id)))

    assert consumer.acked == ["1-0"]
    assert len(execution.failure_calls) == 1
    assert execution.failure_calls[0][1] == FailureKind.RETRYABLE_INFRA


async def test_cancellation_before_effect_commit_leaves_message_pending():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    consumer = RecordingAckConsumer()
    effects = BlockingEffects()
    worker = _worker(task, effects=effects, consumer=consumer)
    running = asyncio.create_task(worker.process(_message(_envelope(task_id, organization_id))))
    await effects.started.wait()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert consumer.acked == []
    assert effects.published == []


async def test_consumer_creates_group_reads_new_and_claims_with_stable_uuid():
    first = _envelope(uuid4(), uuid4())
    redis = RecordingRedis(
        read_result=[(STREAM_NAME, [("4-0", {"envelope": first.canonical_json()})])],
        claim_result=["0-0", [("3-0", {"envelope": first.canonical_json()})], []],
    )
    consumer = RedisInferenceConsumer(redis)
    process_name = consumer.consumer_name

    new_messages = await consumer.read_new()
    stale_messages = await consumer.claim_stale()
    await consumer.ack("4-0")

    assert process_name == consumer.consumer_name
    assert UUID(process_name)
    assert redis.group_calls == [
        ((STREAM_NAME, GROUP_NAME), {"id": "0-0", "mkstream": True})
    ]
    assert redis.read_calls == [
        {
            "groupname": GROUP_NAME,
            "consumername": process_name,
            "streams": {STREAM_NAME: ">"},
            "count": 10,
            "block": 2_000,
        }
    ]
    assert redis.claim_calls[0]["min_idle_time"] == 20_000
    assert redis.claim_calls[0]["consumername"] == process_name
    assert new_messages[0].message_id == "4-0"
    assert stale_messages[0].message_id == "3-0"
    assert redis.ack_calls == [(STREAM_NAME, GROUP_NAME, "4-0")]


async def test_group_creation_only_swallows_busygroup():
    consumer = RedisInferenceConsumer(BusyGroupRedis())
    await consumer.start()

    class OtherErrorRedis(RecordingRedis):
        async def xgroup_create(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        await RedisInferenceConsumer(OtherErrorRedis()).start()
