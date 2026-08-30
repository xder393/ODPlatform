"""Redis/PostgreSQL crash-window coverage for the inference Worker."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(SHARED_SCHEMAS_SRC), str(BACKEND_SRC)]

from odp_schemas.events import EventEnvelope

from odp_api.adapters.persistence.inspection_effects import SqlAlchemyInspectionEffects
from odp_api.adapters.persistence.models import DefectCaseRow
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
    InspectionSessionRow,
    PublishedInferenceResultRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.inspection.effects import InspectionEffectService
from odp_api.modules.tasks.commands import InferenceExecutionContract
from odp_api.modules.tasks.consumer import (
    GROUP_NAME,
    STREAM_NAME,
    RedisInferenceConsumer,
)
from odp_api.modules.tasks.models import TaskStatus
from odp_api.ports.tasks import StaleLease
from odp_api.processes.inference_worker import (
    InferenceResult,
    InferenceWorker,
    LoadedArtifact,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
def anyio_backend():
    return "asyncio"


DATABASE_URL = os.getenv("ODP_POSTGRES_TEST_URL")
REDIS_URL = os.getenv("ODP_REDIS_TEST_URL")


class _Loader:
    def __init__(self, *, started: asyncio.Event | None = None, release: asyncio.Event | None = None):
        self.started = started
        self.release = release

    async def load(self, task):
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        content = b"recovery-frame"
        return LoadedArtifact(content, hashlib.sha256(content).hexdigest())


class _Inference:
    async def infer(self, artifact, task):
        return InferenceResult(
            execution_contract=InferenceExecutionContract(
                "worker-test",
                "b" * 64,
                "test",
                "cpu",
                (1, 3, 32, 32),
                "pre-v1",
                "post-v1",
                0.5,
                0.5,
                "hard",
                False,
                "classes-v1",
            ),
            frame_sha256=artifact.sha256,
            detections=(
                {
                    "defect_type": "scratch",
                    "spatial_zone": "GLOBAL",
                    "severity": "HIGH",
                    "confidence": 0.9,
                },
            ),
        )


class _Lookup:
    def __init__(self, repository):
        self.repository = repository

    def get_task(self, task_id, organization_id):
        return self.repository.get_task(task_id, organization_id)


class _AckRecorder:
    def __init__(self):
        self.acked = []

    async def ack(self, message_id):
        self.acked.append(message_id)


async def _run_worker(
    sessions,
    consumer,
    task_execution,
    *,
    effects,
    lookup,
    loader=None,
):
    return InferenceWorker(
        consumer,
        task_execution,
        effects,
        task_repository=lookup,
        artifact_loader=loader or _Loader(),
        inference=_Inference(),
        worker_id=consumer.consumer_name,
    )


def _seed_task(sessions):
    organization_id, camera_id = uuid4(), uuid4()
    session_id, artifact_id, task_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    frame_sha = hashlib.sha256(b"recovery-frame").hexdigest()
    with sessions.begin() as session:
        session.add_all(
            [
                InspectionSessionRow(
                    session_id=session_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    line_id=uuid4(),
                    source_type="TEST",
                    sanitized_uri="fixture://recovery",
                    status="RUNNING",
                    idempotency_key=str(session_id),
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                FrameArtifactRow(
                    artifact_id=artifact_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    stream_session_id=session_id,
                    frame_sequence=1,
                    captured_at=now,
                    object_key="organizations/test/artifacts/recovery-frame",
                    sha256=frame_sha,
                    content_length=len(b"recovery-frame"),
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=task_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    artifact_id=artifact_id,
                    idempotency_key=str(task_id),
                    status=TaskStatus.READY.value,
                    dispatch_seq=1,
                    attempt_count=0,
                    fence_token=0,
                    created_at=now,
                    updated_at=now,
                ),
                CameraInferenceStateRow(
                    organization_id=organization_id,
                    camera_id=camera_id,
                    running_task_id=None,
                    ready_count=1,
                    version=0,
                    updated_at=now,
                ),
            ]
        )
    return organization_id, camera_id, task_id


def _envelope(task_id, organization_id):
    return EventEnvelope(
        event_id=uuid4(),
        event_type="vision.inference.requested.v1",
        schema_version=1,
        occurred_at=datetime.now(UTC),
        correlation_id=uuid4(),
        organization_id=organization_id,
        aggregate_id=task_id,
        payload={"task_id": str(task_id), "dispatch_seq": 1},
    ).canonical_json()


@pytest.mark.skipif(
    not DATABASE_URL or not REDIS_URL,
    reason="requires the dedicated ODP_POSTGRES_TEST_URL and ODP_REDIS_TEST_URL CI services",
)
async def test_worker_a_cancellation_and_worker_b_xautoclaim_recover_once():
    """A pre-commit crash leaves PEL work for B, which publishes one result."""

    engine, sessions = create_engine_and_session(DATABASE_URL)
    redis = Redis.from_url(REDIS_URL, decode_responses=False)
    await redis.delete(STREAM_NAME)
    try:
        organization_id, _camera_id, task_id = _seed_task(sessions)
        message_id = await redis.xadd(
            STREAM_NAME,
            {"envelope": _envelope(task_id, organization_id).encode()},
        )
        execution = SqlAlchemyTaskControlRepository(sessions)
        effects = InspectionEffectService(SqlAlchemyInspectionEffects(sessions))
        lookup = _Lookup(execution)
        consumer_a = RedisInferenceConsumer(redis, consumer_name=str(uuid4()))
        loader_a_started = asyncio.Event()
        loader_a_release = asyncio.Event()
        worker_a = await _run_worker(
            sessions,
            consumer_a,
            execution,
            effects=effects,
            lookup=lookup,
            loader=_Loader(started=loader_a_started, release=loader_a_release),
        )
        await consumer_a.start()
        first = (await consumer_a.read_new())[0]
        pending = asyncio.create_task(worker_a.process(first))
        await asyncio.wait_for(loader_a_started.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await redis.xpending_range(STREAM_NAME, GROUP_NAME, min="-", max="+", count=10)

        # XAUTOCLAIM's production threshold is 20 seconds.  This remains
        # below the documented 30-second recovery budget including DB fencing.
        await asyncio.sleep(20.2)
        consumer_b = RedisInferenceConsumer(redis, consumer_name=str(uuid4()))
        worker_b = await _run_worker(
            sessions,
            consumer_b,
            execution,
            effects=effects,
            lookup=lookup,
        )
        recovered = await consumer_b.claim_stale()
        assert [item.message_id for item in recovered] == [
            message_id.decode() if isinstance(message_id, bytes) else str(message_id)
        ]
        await worker_b.process(recovered[0])

        with sessions() as session:
            assert session.scalar(
                select(func.count()).select_from(PublishedInferenceResultRow).where(
                    PublishedInferenceResultRow.task_id == task_id
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(DefectCaseRow).where(
                    DefectCaseRow.organization_id == organization_id
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(InferenceAttemptRow).where(
                    InferenceAttemptRow.task_id == task_id
                )
            ) == 2
            assert session.get(InferenceTaskRow, task_id).status == TaskStatus.SUCCEEDED.value
        await consumer_b.close()
        await consumer_a.close()
    finally:
        await redis.delete(STREAM_NAME)
        await redis.aclose()
        engine.dispose()


@pytest.mark.skipif(
    not DATABASE_URL or not REDIS_URL,
    reason="requires the dedicated ODP_POSTGRES_TEST_URL and ODP_REDIS_TEST_URL CI services",
)
async def test_commit_before_ack_redelivery_is_terminal_duplicate_without_new_attempt():
    """A crash after effect commit is absorbed as a terminal duplicate."""

    engine, sessions = create_engine_and_session(DATABASE_URL)
    redis = Redis.from_url(REDIS_URL, decode_responses=False)
    await redis.delete(STREAM_NAME)
    try:
        organization_id, _camera_id, task_id = _seed_task(sessions)
        await redis.xadd(
            STREAM_NAME,
            {"envelope": _envelope(task_id, organization_id).encode()},
        )
        execution = SqlAlchemyTaskControlRepository(sessions)
        real_effects = InspectionEffectService(SqlAlchemyInspectionEffects(sessions))

        class CrashAfterCommit:
            def publish(self, command):
                real_effects.publish(command)
                raise RuntimeError("crash after commit before XACK")

        consumer_a = RedisInferenceConsumer(redis, consumer_name=str(uuid4()))
        worker_a = await _run_worker(
            sessions,
            consumer_a,
            execution,
            effects=CrashAfterCommit(),
            lookup=_Lookup(execution),
        )
        message = (await consumer_a.read_new())[0]
        with pytest.raises(StaleLease):
            await worker_a.process(message)

        with sessions() as session:
            attempts_before = session.scalar(
                select(func.count()).select_from(InferenceAttemptRow).where(
                    InferenceAttemptRow.task_id == task_id
                )
            )
            assert attempts_before == 1
        await asyncio.sleep(20.2)
        consumer_b = RedisInferenceConsumer(redis, consumer_name=str(uuid4()))
        worker_b = await _run_worker(
            sessions,
            consumer_b,
            execution,
            effects=real_effects,
            lookup=_Lookup(execution),
        )
        recovered = await consumer_b.claim_stale()
        assert recovered
        await worker_b.process(recovered[0])

        with sessions() as session:
            assert session.scalar(
                select(func.count()).select_from(PublishedInferenceResultRow).where(
                    PublishedInferenceResultRow.task_id == task_id
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(InferenceAttemptRow).where(
                    InferenceAttemptRow.task_id == task_id
                )
            ) == attempts_before
        await consumer_b.close()
        await consumer_a.close()
    finally:
        await redis.delete(STREAM_NAME)
        await redis.aclose()
        engine.dispose()
