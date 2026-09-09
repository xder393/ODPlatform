"""Admission must not inherit another tenant's same-camera backlog."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    InferenceTaskRow,
)
from odp_api.modules.ingestion.service import IngestionService
from odp_api.modules.tasks.models import TaskRecord, TaskStatus
from odp_api.ports.frame_sources import DecodedFrame
from odp_api.processes.runtime import DatabaseIngestionHealth


@pytest.mark.parametrize("own_count", [None, 0, 1])
def test_foreign_backlog_does_not_reject_or_contaminate_local_admission(own_count):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    foreign, local, camera = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    observed = []

    class Source:
        async def frames(self):
            yield DecodedFrame(camera, uuid4(), 1, now, b"raw")

    class Saga:
        def ingest(self, selected, health, timestamp):
            observed.append((selected.organization_id, health))
            return TaskRecord(
                task_id=uuid4(), task_type="vision_inference", idempotency_key="one",
                payload={}, status=TaskStatus.READY, attempt_count=0, created_at=timestamp,
            )

    try:
        with sessions.begin() as session:
            session.add(CameraInferenceStateRow(
                organization_id=foreign, camera_id=camera, ready_count=2,
            ))
            if own_count is not None:
                session.add(CameraInferenceStateRow(
                    organization_id=local, camera_id=camera, ready_count=own_count,
                ))
            # These rows exercise the actual SQL predicates; artifact contents
            # are not consumed by the health query.
            for tenant, status, age in [
                (foreign, "READY", 60), (local, "SUCCEEDED", 120),
            ]:
                session.add(InferenceTaskRow(
                    task_id=uuid4(), organization_id=tenant, camera_id=camera,
                    artifact_id=uuid4(), idempotency_key=status, status=status,
                    created_at=now - timedelta(seconds=age),
                ))
        health = DatabaseIngestionHealth(
            sessions, SimpleNamespace(get=lambda key: "alive"), "a" * 64,
        )
        report = asyncio.run(IngestionService(
            SimpleNamespace(encode=lambda frame: b"jpeg"), clock=lambda: now,
        ).run(Source(), organization_id=local, health=health, saga=Saga(), max_frames=1))
        assert report.admitted == 1
        assert report.rejected == 0
        assert observed[0][0] == local
        assert observed[0][1].ready_count == (own_count or 0)
        assert observed[0][1].oldest_ready_age_seconds == 0
    finally:
        engine.dispose()
