"""Concrete dependency assembly for the independently deployed P1 processes.

The process classes intentionally remain dependency-injected and easy to test.
This module is the only place that knows how the deployed PostgreSQL, Redis,
MinIO, OpenCV, and ONNX adapters are assembled.  Keeping that wiring here
prevents a process entrypoint from quietly falling back to an in-memory demo
adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from odp_api.adapters.events.redis_streams import RedisOutboxPublisher
from odp_api.adapters.frame_sources.opencv import (
    LocalCameraSource,
    OpenCvJpegEncoder,
    RecordedVideoSource,
    RtspSource,
)
from odp_api.adapters.persistence.inspection_effects import SqlAlchemyInspectionEffects
from odp_api.adapters.persistence.inspection_sessions import (
    SqlAlchemyInspectionSessionRepository,
)
from odp_api.adapters.persistence.outbox import SqlAlchemyOutboxRepository
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceTaskRow,
)
from odp_api.adapters.redis_stream import RedisSocketStreamClient
from odp_api.adapters.storage.minio import MinioObjectStorage
from odp_api.adapters.vision.onnx import OnnxVisionAdapter
from odp_api.db import create_engine_and_session
from odp_api.modules.ingestion.artifacts import ArtifactHealth, ArtifactSaga
from odp_api.modules.ingestion.service import IngestionHealthPort, IngestionService
from odp_api.modules.tasks.commands import LeaseClaim
from odp_api.modules.tasks.consumer import RedisInferenceConsumer
from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope
from odp_api.modules.tasks.retention import (
    ALERT_STREAM_NAME,
    INFERENCE_STREAM_NAME,
    GatewayGroupExpiryRegistry,
    MinimumRetentionPolicy,
    RedisRetentionAdapter,
    StreamRetentionController,
    StreamRetentionPolicy,
)
from odp_api.ports.frame_sources import FrameSource
from odp_api.ports.storage import ObjectStoragePort
from odp_api.ports.vision import FrameInput, VisionInferencePort
from odp_api.processes.frame_ingestor import FrameIngestor
from odp_api.processes.inference_worker import (
    ArtifactLoaderPort,
    InferencePort,
    InferenceResult,
    InferenceWorker,
    LoadedArtifact,
    ThreadedInspectionEffectAdapter,
    ThreadedTaskControlAdapter,
)
from odp_api.processes.outbox_relay import OutboxRelay, OutboxRelayProcess
from odp_api.processes.recovery_scheduler import (
    PostgreSqlAdvisoryLock,
    RecoveryScheduler,
)
from odp_api.processes.stream_retention import StreamRetentionProcess
from odp_api.processes.worker_liveness import RedisWorkerHeartbeat, liveness_key
from odp_api.settings import (
    IngestorSettings,
    ProcessSettings,
    RelaySettings,
    SchedulerSettings,
    StreamRetentionSettings,
    WorkerSettings,
)


def build_database(settings: ProcessSettings):
    """Create the process-owned SQLAlchemy engine and session factory."""

    if not settings.database_url:
        raise ValueError("ODP_DATABASE_URL is required")
    return create_engine_and_session(settings.database_url)


def build_storage(settings: ProcessSettings) -> MinioObjectStorage:
    """Create an internal MinIO client; browser signing is API-only."""

    required = {
        "ODP_MINIO_ENDPOINT": settings.minio_endpoint,
        "ODP_MINIO_ACCESS_KEY": settings.minio_access_key,
        "ODP_MINIO_SECRET_KEY": settings.minio_secret_key,
        "ODP_MINIO_BUCKET": settings.minio_bucket,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"process storage settings missing: {', '.join(missing)}")
    return MinioObjectStorage(
        settings.minio_endpoint or "",
        settings.minio_access_key or "",
        settings.minio_secret_key or "",
        settings.minio_bucket or "",
        secure=settings.minio_secure,
        region=settings.minio_region,
    )


def build_redis(settings: ProcessSettings) -> tuple[RedisSocketStreamClient, Any]:
    """Create separate sync and async Redis clients for blocking boundaries."""

    if not settings.redis_url:
        raise ValueError("ODP_REDIS_URL is required")
    sync = RedisSocketStreamClient(settings.redis_url)
    return sync, sync.async_client()


class DatabaseArtifactLoader(ArtifactLoaderPort):
    """Load only the tenant/task/artifact chain authorized by the DB lease."""

    def __init__(self, sessions, storage: ObjectStoragePort) -> None:
        self._sessions = sessions
        self._storage = storage

    async def load(self, claim: LeaseClaim) -> LoadedArtifact:
        return await asyncio.to_thread(self._load_sync, claim)

    def _load_sync(self, claim: LeaseClaim) -> LoadedArtifact:
        with self._sessions() as session:
            task = session.scalar(
                select(InferenceTaskRow).where(
                    InferenceTaskRow.task_id == claim.task_id,
                    InferenceTaskRow.organization_id == claim.organization_id,
                    InferenceTaskRow.artifact_id == claim.artifact_id,
                )
            )
            if task is None:
                raise ValueError("inference task is outside the claimed tenant scope")
            artifact = session.scalar(
                select(FrameArtifactRow).where(
                    FrameArtifactRow.artifact_id == claim.artifact_id,
                    FrameArtifactRow.organization_id == claim.organization_id,
                    FrameArtifactRow.camera_id == task.camera_id,
                    FrameArtifactRow.state == "AVAILABLE",
                )
            )
            if artifact is None or not artifact.object_key or not artifact.sha256:
                raise ValueError("claimed artifact is not available")
            object_key = artifact.object_key
            expected_sha = artifact.sha256.lower()
        content = bytes(self._storage.get(object_key))
        actual_sha = hashlib.sha256(content).hexdigest()
        if actual_sha != expected_sha:
            raise ValueError("artifact bytes do not match the persisted SHA-256")
        return LoadedArtifact(content=content, sha256=actual_sha, object_key=object_key)


class VisionInferenceAdapter(InferencePort):
    """Offload the deterministic ONNX adapter and preserve its full contract."""

    def __init__(self, vision: VisionInferencePort) -> None:
        self._vision = vision

    async def infer(self, artifact: LoadedArtifact, claim: LeaseClaim) -> InferenceResult:
        result = await asyncio.to_thread(
            self._vision.inspect,
            FrameInput(fixture_name=str(claim.artifact_id), content=artifact.content),
        )
        return InferenceResult(
            execution_contract=result.execution,
            frame_sha256=result.frame_sha256,
            detections=result.detections,
            stage_durations=result.stage_durations,
        )


class DatabaseIngestionHealth(IngestionHealthPort):
    """Snapshot admission pressure and transport health before encoding."""

    def __init__(self, sessions, redis: RedisSocketStreamClient, model_sha256: str) -> None:
        self._sessions = sessions
        self._redis = redis
        self._liveness_key = liveness_key(model_sha256)

    async def snapshot(self, organization_id: UUID, camera_id: UUID) -> ArtifactHealth:
        return await asyncio.to_thread(self._snapshot, organization_id, camera_id)

    def _snapshot(self, organization_id: UUID, camera_id: UUID) -> ArtifactHealth:
        with self._sessions() as session:
            state = session.scalar(
                select(CameraInferenceStateRow).where(
                    CameraInferenceStateRow.organization_id == organization_id,
                    CameraInferenceStateRow.camera_id == camera_id,
                )
            )
            ready_count = int(state.ready_count) if state is not None else 0
            oldest = session.scalar(
                select(func.min(InferenceTaskRow.created_at)).where(
                    InferenceTaskRow.organization_id == organization_id,
                    InferenceTaskRow.camera_id == camera_id,
                    InferenceTaskRow.status == "READY",
                )
            )
        try:
            worker_healthy = self._redis.get(self._liveness_key) == "alive"
            redis_available = True
        except Exception:  # noqa: BLE001 - health is deliberately fail-closed
            redis_available = False
            worker_healthy = False
        age = 0.0
        if oldest is not None:
            value = oldest.replace(tzinfo=UTC) if oldest.tzinfo is None else oldest.astimezone(UTC)
            age = max(0.0, (datetime.now(UTC) - value).total_seconds())
        return ArtifactHealth(
            worker_healthy=worker_healthy,
            redis_available=redis_available,
            ready_count=ready_count,
            oldest_ready_age_seconds=age,
        )


def source_from_session(session) -> FrameSource:
    """Resolve only the three validated source types persisted by the API."""

    source_type = str(session.source_type).strip().upper()
    kwargs = {"camera_id": session.camera_id, "session_id": session.session_id}
    if source_type == "RECORDED":
        return RecordedVideoSource(session.sanitized_uri, **kwargs)
    if source_type == "RTSP":
        return RtspSource(session.sanitized_uri, **kwargs)
    if source_type == "LOCAL_CAMERA":
        try:
            device_index = int(session.sanitized_uri)
        except (TypeError, ValueError) as error:
            raise ValueError("local camera source must use a numeric device index") from error
        return LocalCameraSource(device_index, **kwargs)
    raise ValueError(f"unsupported inspection source type: {source_type}")


def build_ingestor(settings: IngestorSettings) -> FrameIngestor:
    engine, sessions = build_database(settings)
    _ = engine
    storage = build_storage(settings)
    _verify_model_file(settings.model_path, settings.model_sha256)
    repository = SqlAlchemyTaskControlRepository(sessions)
    session_repository = SqlAlchemyInspectionSessionRepository(sessions)
    if not settings.redis_url:
        raise ValueError("ODP_REDIS_URL is required")
    health = DatabaseIngestionHealth(
        sessions, RedisSocketStreamClient(settings.redis_url), settings.model_sha256 or ""
    )
    ingestion = IngestionService(OpenCvJpegEncoder())
    return FrameIngestor(
        session_repository,
        ingestion,
        health,
        source_from_session,
        lambda _session: ArtifactSaga(repository, storage),
        process_id=_process_id("frame-ingestor"),
        instance_id=uuid4(),
        heartbeat_interval_seconds=max(1.0, settings.lease_seconds / 4),
    )


def build_worker(settings: WorkerSettings) -> InferenceWorker:
    engine, sessions = build_database(settings)
    _ = engine
    storage = build_storage(settings)
    _sync_redis, async_redis = build_redis(settings)
    consumer = RedisInferenceConsumer(async_redis, consumer_name=_process_id("inference-worker"))
    repository = SqlAlchemyTaskControlRepository(sessions)
    worker_id = _process_id("inference-worker")
    effects = SqlAlchemyInspectionEffects(sessions)
    model = OnnxVisionAdapter(
        settings.model_path or "",
        model_sha256=settings.model_sha256 or "",
        providers=(settings.execution_provider,),
    )
    return InferenceWorker(
        consumer,
        ThreadedTaskControlAdapter(repository, worker_id),
        ThreadedInspectionEffectAdapter(effects),
        DatabaseArtifactLoader(sessions, storage),
        VisionInferenceAdapter(model),
        worker_id=worker_id,
        renewal_interval_seconds=max(1.0, settings.lease_seconds / 4),
        recovery_interval_seconds=float(settings.recovery_loop_seconds),
        heartbeat=RedisWorkerHeartbeat(async_redis, settings.model_sha256 or ""),
    )


def build_relay(settings: RelaySettings) -> OutboxRelayProcess:
    engine, sessions = build_database(settings)
    _ = engine
    if not settings.redis_url:
        raise ValueError("ODP_REDIS_URL is required")
    sync_redis = RedisSocketStreamClient(settings.redis_url)
    relay = OutboxRelay(
        SqlAlchemyOutboxRepository(sessions),
        RedisOutboxPublisher(sync_redis),
        relay_id=_process_id("outbox-relay"),
    )
    return OutboxRelayProcess(relay)


def build_recovery_scheduler(settings: SchedulerSettings) -> RecoveryScheduler:
    engine, sessions = build_database(settings)
    repository = SqlAlchemyTaskControlRepository(sessions)
    return RecoveryScheduler(
        RecoveryService(repository, SystemRecoveryScope(_process_id("recovery-scheduler"))),
        PostgreSqlAdvisoryLock(engine),
        clock=lambda: datetime.now(UTC),
        interval_seconds=float(settings.recovery_loop_seconds),
    )


def build_retention(settings: StreamRetentionSettings) -> tuple[StreamRetentionProcess, Any]:
    _sync_redis, async_redis = build_redis(settings)
    adapter = RedisRetentionAdapter(async_redis)
    now = lambda: datetime.now(UTC)
    controllers = (
        StreamRetentionController(
            adapter,
            StreamRetentionPolicy(
                INFERENCE_STREAM_NAME,
                MinimumRetentionPolicy(timedelta(minutes=15)),
            ),
            clock=now,
        ),
        StreamRetentionController(
            adapter,
            StreamRetentionPolicy(
                ALERT_STREAM_NAME,
                MinimumRetentionPolicy(timedelta(hours=24)),
                GatewayGroupExpiryRegistry(()),
            ),
            clock=now,
        ),
    )
    return StreamRetentionProcess(controllers), async_redis


def _process_id(role: str) -> str:
    from odp_api.processes.common import process_id

    return process_id(role)


def _verify_model_file(model_path: str | None, expected_sha256: str | None) -> None:
    if not model_path or not expected_sha256:
        raise ValueError("model path and SHA-256 are required")
    path = Path(model_path)
    if not path.is_file():
        raise ValueError("configured model file does not exist")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest.lower() != expected_sha256.lower():
        raise ValueError("configured model SHA-256 does not match the model file")


__all__ = [
    "DatabaseArtifactLoader",
    "DatabaseIngestionHealth",
    "VisionInferenceAdapter",
    "build_database",
    "build_ingestor",
    "build_recovery_scheduler",
    "build_redis",
    "build_relay",
    "build_retention",
    "build_storage",
    "build_worker",
    "source_from_session",
]
