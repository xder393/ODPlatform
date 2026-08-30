"""Behavioral tests for Redis inference delivery and fenced Worker execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

BACKEND_SRC = Path(__file__).parents[3] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[5] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(SHARED_SCHEMAS_SRC), str(BACKEND_SRC)]

from odp_schemas.events import EventEnvelope

from odp_api.modules.tasks.commands import (
    DeliveryDecision,
    DeliveryOutcome,
    InferenceExecutionContract,
    LeaseClaim,
)
from odp_api.modules.tasks.consumer import (
    GROUP_NAME,
    STREAM_NAME,
    RedisInferenceConsumer,
    RedisInferenceMessage,
)
from odp_api.modules.tasks.models import FailureKind, TaskRecord, TaskStatus
from odp_api.modules.tasks.recovery import QuarantineResult
from odp_api.processes.inference_worker import (
    InferenceResult,
    InferenceWorker,
    LoadedArtifact,
)

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


def _message(envelope: EventEnvelope, message_id: str = "1-0") -> RedisInferenceMessage:
    return RedisInferenceMessage(
        message_id=message_id,
        fields={"envelope": envelope.canonical_json().encode()},
        stream=STREAM_NAME,
    )


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
    worker_id = "worker-a"

    def __init__(self, claim_result, *, task=None):
        self.claim_result = claim_result
        self.task = task
        self.claim_calls = []
        self.renew_calls = []
        self.failure_calls = []
        self.delivery_requests = []
        self.unscoped = []

    async def accept_delivery(self, request, now):
        self.claim_calls.append((request, now))
        self.delivery_requests.append(request)
        if self.task is not None:
            if self.task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.DEAD_LETTER,
                TaskStatus.BLOCKED_COMPATIBILITY,
                TaskStatus.SKIPPED_STALE,
                TaskStatus.SKIPPED_BACKPRESSURE,
            }:
                return DeliveryDecision(DeliveryOutcome.DUPLICATE)
            if request.expected_dispatch_seq < self.task.dispatch_seq:
                return DeliveryDecision(DeliveryOutcome.DUPLICATE)
            if request.expected_dispatch_seq > self.task.dispatch_seq:
                return DeliveryDecision(DeliveryOutcome.QUARANTINED)
        if request.quarantine_reason is not None:
            return DeliveryDecision(DeliveryOutcome.QUARANTINED)
        if self.claim_result is None:
            return DeliveryDecision(DeliveryOutcome.PENDING)
        return DeliveryDecision(DeliveryOutcome.CLAIMED, self.claim_result)

    async def quarantine_unscoped(self, command, now):
        self.unscoped.append((command, now))
        return QuarantineResult(uuid4(), ack_after_commit=True)

    async def renew(self, *args):
        self.renew_calls.append(args)
        return args[0]

    async def record_failure(self, *args):
        self.failure_calls.append(args)


class FakeArtifactLoader:
    async def load(self, claim):
        return LoadedArtifact(b"frame", hashlib.sha256(b"frame").hexdigest())


class FakeInference:
    async def infer(self, artifact, claim):
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

    async def start(self):
        return None

    async def claim_stale(self):
        return []

    async def read_new(self):
        return []

    async def close(self):
        return None


def _worker(task, *, effects, execution=None, consumer=None, loader=None, inference=None):
    return InferenceWorker(
        consumer or RecordingAckConsumer(),
        execution or FakeExecution(_claim(task), task=task),
        effects,
        loader or FakeArtifactLoader(),
        inference or FakeInference(),
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
    execution = FakeExecution(None, task=task)
    consumer = RecordingAckConsumer()
    effects = RecordingEffects()
    worker = _worker(task, effects=effects, execution=execution, consumer=consumer)

    await worker.process(_message(_envelope(task_id, organization_id)))

    assert len(execution.claim_calls) == 1
    assert effects.published == []
    assert consumer.acked == ["1-0"]


async def test_live_foreign_lease_stays_pending_when_claim_is_rejected():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    execution = FakeExecution(None, task=task)
    consumer = RecordingAckConsumer()
    effects = RecordingEffects()
    worker = _worker(task, effects=effects, execution=execution, consumer=consumer)

    await worker.process(_message(_envelope(task_id, organization_id)))

    assert consumer.acked == []
    assert effects.published == []


async def test_unsupported_schema_is_quarantined_before_ack_with_bounded_payload():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    consumer = RecordingAckConsumer()
    effects = RecordingEffects()

    execution = FakeExecution(None, task=task)
    worker = _worker(task, effects=effects, execution=execution, consumer=consumer)
    envelope = _envelope(task_id, organization_id, schema_version=9)
    raw = json.dumps(
        {
            **json.loads(envelope.canonical_json()),
            "padding": "x" * 100_000,
        }
    ).encode()

    await worker.process(RedisInferenceMessage("2-0", {"envelope": raw}, STREAM_NAME))

    assert len(execution.delivery_requests) == 1
    assert len(execution.delivery_requests[0].raw_payload) <= 64 * 1024
    assert consumer.acked == ["2-0"]


async def test_timeout_is_recorded_as_retryable_failure_before_ack():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    claim = _claim(task)
    execution = FakeExecution(claim, task=task)

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


async def test_dispatch_redispatch_race_cannot_claim_a_newer_database_generation():
    """A message for dispatch 1 must not claim after DB advances to dispatch 2."""

    organization_id, task_id = uuid4(), uuid4()
    stale_view = _task(task_id, organization_id, dispatch_seq=1)
    current_view = _task(task_id, organization_id, dispatch_seq=2)
    claim = _claim(current_view)

    class RacingExecution(FakeExecution):
        def __init__(self):
            super().__init__(claim, task=current_view)

        async def accept_delivery(self, request, now):
            assert stale_view.dispatch_seq == request.expected_dispatch_seq
            return await super().accept_delivery(request, now)

    execution = RacingExecution()
    consumer = RecordingAckConsumer()
    effects = RecordingEffects()
    worker = InferenceWorker(
        consumer,
        execution,
        effects,
        FakeArtifactLoader(),
        FakeInference(),
        clock=lambda: NOW,
        worker_id="worker-a",
    )

    result = await worker.process(_message(_envelope(task_id, organization_id, dispatch_seq=1)))

    assert result.outcome == "DUPLICATE"
    assert effects.published == []
    assert consumer.acked == ["1-0"]


@pytest.mark.parametrize(
    ("raw_payload", "expected_reason"),
    [
        (b"{not-json", "MALFORMED_ENVELOPE"),
        (b"x" * (64 * 1024 + 1), "PAYLOAD_TOO_LARGE"),
    ],
)
async def test_unscoped_poison_message_is_durably_quarantined_then_acked(
    raw_payload, expected_reason
):
    """Missing tenant/task metadata must not poison the Redis PEL forever."""

    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)
    execution = FakeExecution(None, task=task)
    consumer = RecordingAckConsumer()
    worker = _worker(
        task,
        effects=RecordingEffects(),
        execution=execution,
        consumer=consumer,
    )

    result = await worker.process(
        RedisInferenceMessage("poison-1", {"envelope": raw_payload}, STREAM_NAME)
    )

    assert result.outcome == "QUARANTINED"
    assert result.acked is True
    assert len(execution.unscoped) == 1
    assert execution.unscoped[0][0].reason.value == expected_reason
    assert consumer.acked == ["poison-1"]


async def test_quarantine_persists_the_exact_reason_end_to_end():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)

    execution = FakeExecution(None, task=task)
    consumer = RecordingAckConsumer()
    worker = _worker(
        task,
        effects=RecordingEffects(),
        execution=execution,
        consumer=consumer,
    )

    result = await worker.process(
        _message(_envelope(task_id, organization_id, schema_version=9), "reason-1")
    )

    assert result.outcome == "QUARANTINED"
    assert execution.delivery_requests[0].quarantine_reason.value == "UNSUPPORTED_SCHEMA"
    assert consumer.acked == ["reason-1"]


async def test_artifact_loading_is_outside_the_model_inference_timeout():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)

    class SlowArtifactLoader:
        async def load(self, claim):
            await asyncio.sleep(0.03)
            return LoadedArtifact(b"frame", hashlib.sha256(b"frame").hexdigest())

    execution = FakeExecution(_claim(task), task=task)
    consumer = RecordingAckConsumer()
    worker = InferenceWorker(
        consumer,
        execution,
        RecordingEffects(),
        SlowArtifactLoader(),
        FakeInference(),
        clock=lambda: NOW,
        worker_id="worker-a",
        renewal_interval_seconds=0.005,
        inference_timeout_seconds=0.01,
    )

    result = await worker.process(_message(_envelope(task_id, organization_id)))

    assert result.outcome == "SUCCEEDED"
    assert execution.failure_calls == []
    assert execution.renew_calls
    assert consumer.acked == ["1-0"]


async def test_incomplete_inference_output_is_model_configuration_failure():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)

    class IncompleteInference:
        async def infer(self, artifact, claim):
            return {}

    execution = FakeExecution(_claim(task), task=task)
    consumer = RecordingAckConsumer()
    worker = _worker(
        task,
        effects=RecordingEffects(),
        execution=execution,
        consumer=consumer,
        inference=IncompleteInference(),
    )

    result = await worker.process(_message(_envelope(task_id, organization_id)))

    assert result.outcome == "FAILED"
    assert execution.failure_calls[0][1] == FailureKind.MODEL_CONFIGURATION
    assert consumer.acked == ["1-0"]


async def test_malformed_typed_inference_contract_is_model_configuration_failure():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)

    class MalformedTypedInference:
        async def infer(self, artifact, claim):
            return InferenceResult(
                execution_contract=replace(CONTRACT, model_release=None),
                frame_sha256=artifact.sha256,
                detections=(),
                stage_durations=(("model", 0.1),),
            )

    execution = FakeExecution(_claim(task), task=task)
    consumer = RecordingAckConsumer()
    worker = _worker(
        task,
        effects=RecordingEffects(),
        execution=execution,
        consumer=consumer,
        inference=MalformedTypedInference(),
    )

    result = await worker.process(_message(_envelope(task_id, organization_id)))

    assert result.outcome == "FAILED"
    assert execution.failure_calls[0][1] == FailureKind.MODEL_CONFIGURATION
    assert consumer.acked == ["1-0"]


async def test_run_once_isolates_one_delivery_exception_and_processes_the_next():
    organization_id, task_id = uuid4(), uuid4()
    task = _task(task_id, organization_id)

    class FlakyExecution(FakeExecution):
        def __init__(self):
            super().__init__(_claim(task), task=task)
            self.calls = 0

        async def accept_delivery(self, request, now):
            self.calls += 1
            if self.calls == 1:
                raise TypeError("database driver decoded an invalid column")
            return await super().accept_delivery(request, now)

    class BatchConsumer(RecordingAckConsumer):
        async def read_new(self):
            return [
                _message(_envelope(task_id, organization_id), "first-1"),
                _message(_envelope(task_id, organization_id), "second-1"),
            ]

    consumer = BatchConsumer()
    execution = FlakyExecution()
    worker = InferenceWorker(
        consumer,
        execution,
        RecordingEffects(),
        FakeArtifactLoader(),
        FakeInference(),
        clock=lambda: NOW,
        worker_id="worker-a",
    )

    processed = await worker.run_once()

    assert processed == 2
    assert consumer.acked == ["second-1"]
    assert execution.unscoped == []


async def test_structurally_invalid_redis_fields_become_quarantinable_delivery():
    """One poison entry must not make transport normalization kill the Worker loop."""

    redis = RecordingRedis(
        read_result=[(STREAM_NAME, [("bad-fields-1", ["envelope"])])],
        claim_result=["0-0", [], []],
    )
    consumer = RedisInferenceConsumer(redis, consumer_name=str(uuid4()))
    execution = FakeExecution(None)
    worker = InferenceWorker(
        consumer,
        execution,
        RecordingEffects(),
        FakeArtifactLoader(),
        FakeInference(),
        worker_id="worker-a",
        clock=lambda: NOW,
    )

    processed = await worker.run_once()

    assert processed == 1
    assert execution.unscoped[0][0].reason.value == "MALFORMED_ENVELOPE"
    assert redis.ack_calls == [(STREAM_NAME, GROUP_NAME, "bad-fields-1")]
