"""Recorded AVI -> MinIO -> PostgreSQL Outbox -> Redis -> ONNX -> durable alert.

Uses real providers in an isolated schema, bucket and streams. The fixed-output
model verifies plumbing, not accuracy. This does not exercise Compose roles/UI.
"""

import asyncio
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from odp_api.adapters.events.redis_streams import RedisOutboxPublisher
from odp_api.adapters.frame_sources.opencv import OpenCvJpegEncoder
from odp_api.adapters.persistence.inspection_effects import SqlAlchemyInspectionEffects
from odp_api.adapters.persistence.inspection_sessions import (
    SqlAlchemyInspectionSessionRepository,
)
from odp_api.adapters.persistence.models import (
    AlertRow,
    AuditLogRow,
    Base,
    DefectCaseRow,
)
from odp_api.adapters.persistence.outbox import SqlAlchemyOutboxRepository
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    PublishedInferenceResultRow,
)
from odp_api.adapters.redis_stream import RedisSocketStreamClient
from odp_api.adapters.storage.minio import MinioObjectStorage
from odp_api.adapters.vision.onnx import OnnxVisionAdapter
from odp_api.modules.ingestion.artifacts import ArtifactSaga
from odp_api.modules.ingestion.service import IngestionService
from odp_api.modules.tasks.consumer import RedisInferenceConsumer
from odp_api.processes.frame_ingestor import FrameIngestor
from odp_api.processes.inference_worker import (
    InferenceWorker,
    ThreadedInspectionEffectAdapter,
    ThreadedTaskControlAdapter,
)
from odp_api.processes.outbox_relay import OutboxRelay
from odp_api.processes.runtime import (
    DatabaseArtifactLoader,
    DatabaseIngestionHealth,
    VisionInferenceAdapter,
    source_from_session,
)
from odp_api.processes.worker_liveness import RedisWorkerHeartbeat, liveness_key

REQUIRED = (
    "ODP_POSTGRES_TEST_URL", "ODP_REDIS_TEST_URL", "ODP_MINIO_TEST_ENDPOINT",
    "ODP_MINIO_TEST_ACCESS_KEY", "ODP_MINIO_TEST_SECRET_KEY",
)


@pytest.mark.skipif(not all(os.getenv(key) for key in REQUIRED), reason="requires PG/Redis/MinIO test services")
def test_recorded_video_creates_persistent_alert_and_duplicate_is_harmless(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from minio import Minio
    from redis.asyncio import Redis

    video = tmp_path / "surface.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 5, (64, 64))
    assert writer.isOpened()
    try:
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        cv2.line(frame, (5, 5), (55, 55), (255, 255, 255), 2)
        writer.write(frame)
    finally:
        writer.release()

    token = uuid4().hex
    schema, bucket = f"video_{token}", f"video-{token}"
    engine = create_engine(os.environ[REQUIRED[0]])
    scoped = engine.execution_options(schema_translate_map={None: schema})
    sessions = sessionmaker(scoped, expire_on_commit=False)
    client = Minio(
        os.environ[REQUIRED[2]], access_key=os.environ[REQUIRED[3]],
        secret_key=os.environ[REQUIRED[4]], secure=False,
    )
    storage = MinioObjectStorage(
        os.environ[REQUIRED[2]], os.environ[REQUIRED[3]], os.environ[REQUIRED[4]],
        bucket, secure=False, client=client,
    )
    model_path = Path(__file__).parents[1] / "fixtures/models/tiny-detector.onnx"
    digest = sha256(model_path.read_bytes()).hexdigest()
    # Load before capturing: model startup must not consume the frame TTL.
    model = OnnxVisionAdapter(model_path, model_sha256=digest)
    created_schema = created_bucket = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created_schema = True
        Base.metadata.create_all(scoped)
        client.make_bucket(bucket)
        created_bucket = True
        organization, camera, session_id = uuid4(), uuid4(), uuid4()
        with sessions.begin() as session:
            session.add(InspectionSessionRow(
                session_id=session_id, organization_id=organization, camera_id=camera,
                line_id=uuid4(), source_type="RECORDED", sanitized_uri=str(video),
                status="START_REQUESTED", idempotency_key=token,
            ))

        async def pipeline():
            redis = Redis.from_url(os.environ[REQUIRED[1]])
            streams = (f"video:{token}:tasks", f"video:{token}:alerts")
            # Isolate test presence from any deployed model's presence.
            presence_digest = sha256(token.encode()).hexdigest()
            sync = RedisSocketStreamClient(os.environ[REQUIRED[1]])
            control = SqlAlchemyTaskControlRepository(sessions)
            consumer = RedisInferenceConsumer(redis, stream_name=streams[0], block_ms=10)
            worker = InferenceWorker(
                consumer, ThreadedTaskControlAdapter(control, consumer.consumer_name),
                ThreadedInspectionEffectAdapter(SqlAlchemyInspectionEffects(sessions)),
                DatabaseArtifactLoader(sessions, storage), VisionInferenceAdapter(model),
            )
            try:
                await consumer.start()
                await RedisWorkerHeartbeat(redis, presence_digest)()
                ingestor = FrameIngestor(
                    SqlAlchemyInspectionSessionRepository(sessions),
                    IngestionService(OpenCvJpegEncoder()),
                    DatabaseIngestionHealth(sessions, sync, presence_digest),
                    source_from_session, lambda _: ArtifactSaga(control, storage),
                    process_id=f"ingestor-{token}",
                    instance_id=uuid4(),
                )
                report = await ingestor.run_once(max_frames_per_session=1)
                assert report.failed == 0
                assert len(report.reports) == 1
                assert report.reports[0].admitted == 1, report.reports[0].rejection_reasons
                relay = OutboxRelay(SqlAlchemyOutboxRepository(sessions), RedisOutboxPublisher(
                    sync, {"vision.inference.requested.v1": streams[0],
                           "inspection.alert.created.v1": streams[1]},
                ))
                result = await asyncio.to_thread(relay.run_batch, 10, datetime.now(UTC))
                assert result.published == 1
                messages = await consumer.read_new()
                assert len(messages) == 1
                outcome = await worker.process(messages[0])
                assert outcome.outcome == "SUCCEEDED", outcome
                duplicate = await worker.process(messages[0])
                assert duplicate.outcome == "DUPLICATE"
                assert (await redis.xpending(streams[0], consumer.group_name))["pending"] == 0
                assert (await asyncio.to_thread(relay.run_batch, 10, datetime.now(UTC))).published == 1
                assert await redis.xlen(streams[1]) == 1
            finally:
                await redis.delete(*streams, liveness_key(presence_digest))
                await worker.close()

        asyncio.run(pipeline())
        # Reopen the DB pool so persistence is not an in-memory ORM artifact.
        engine.dispose()
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(PublishedInferenceResultRow)) == 1
            assert session.scalar(select(func.count()).select_from(DefectCaseRow)) == 1
            assert session.scalar(select(func.count()).select_from(AlertRow)) == 1
            assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 1
            assert session.scalar(select(InferenceTaskRow.status)) == "SUCCEEDED"
            artifact = session.scalar(select(FrameArtifactRow))
            assert artifact.lifecycle == "EVIDENCE"
            assert sha256(storage.get(artifact.object_key)).hexdigest() == artifact.sha256
            result = session.scalar(select(PublishedInferenceResultRow))
            assert result.model_sha256 == digest
            assert result.frame_sha256 == artifact.sha256
    finally:
        try:
            if created_bucket:
                for item in client.list_objects(bucket, recursive=True):
                    client.remove_object(bucket, item.object_name)
                client.remove_bucket(bucket)
        finally:
            if created_schema:
                Base.metadata.drop_all(scoped)
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema))
            engine.dispose()
