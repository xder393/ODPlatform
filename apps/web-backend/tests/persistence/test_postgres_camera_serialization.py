from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select


WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC)]

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
)
from odp_api.db import create_engine_and_session
from odp_api.ports.tasks import AdmissionRejected, AdmissionRequest


def _request(organization_id, camera_id, session_id, frame_sequence):
    return AdmissionRequest(
        organization_id=organization_id,
        camera_id=camera_id,
        stream_session_id=session_id,
        frame_sequence=frame_sequence,
        captured_at=datetime(2026, 8, 25, 12, 0, frame_sequence, tzinfo=UTC),
        content_sha256=f"{frame_sequence:064x}",
        correlation_id=uuid4(),
    )


@pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL CI database",
)
def test_postgresql_camera_admission_serializes_eviction_and_keeps_two_ready_tasks():
    database_url = os.environ["ODP_POSTGRES_TEST_URL"]
    engine, sessions = create_engine_and_session(database_url)
    organization_id, camera_id, session_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    try:
        Base.metadata.create_all(engine)
        with sessions.begin() as session:
            session.add(
                InspectionSessionRow(
                    session_id=session_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    line_id=uuid4(),
                    source_type="TEST",
                    sanitized_uri="rtsp://test.invalid/camera",
                    status="RUNNING",
                    idempotency_key=str(uuid4()),
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

        repository = SqlAlchemyTaskControlRepository(sessions)
        first = repository.reserve(_request(organization_id, camera_id, session_id, 1), now)
        with pytest.raises(AdmissionRejected, match="RESERVATION_NOT_FOUND"):
            repository.complete_upload(first.reservation_id, uuid4(), "frames/1.jpg", 10, now)
        first_task = repository.complete_upload(first.reservation_id, organization_id, "frames/1.jpg", 10, now)
        second = repository.reserve(_request(organization_id, camera_id, session_id, 2), now)
        second_task = repository.complete_upload(second.reservation_id, organization_id, "frames/2.jpg", 10, now)

        barrier = Barrier(2)

        def admit(frame_sequence: int):
            barrier.wait()
            reservation = repository.reserve(
                _request(organization_id, camera_id, session_id, frame_sequence), now
            )
            return repository.complete_upload(
                reservation.reservation_id,
                organization_id,
                f"frames/{frame_sequence}.jpg",
                10,
                now,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = list(executor.map(admit, (3, 4)))

        assert {task.status for task in tasks} == {"READY"}
        with sessions() as session:
            rows = session.scalars(
                select(InferenceTaskRow)
                .where(
                    InferenceTaskRow.organization_id == organization_id,
                    InferenceTaskRow.camera_id == camera_id,
                )
                .order_by(InferenceTaskRow.created_at, InferenceTaskRow.task_id)
            ).all()
            ready = [row for row in rows if row.status == "READY"]
            assert len(ready) == 2
            assert session.get(InferenceTaskRow, first_task.task_id).status == "SKIPPED_BACKPRESSURE"
            assert session.get(InferenceTaskRow, second_task.task_id).status == "SKIPPED_BACKPRESSURE"
            assert all(row.status in {"READY", "SKIPPED_BACKPRESSURE"} for row in rows)
            artifacts = session.scalars(
                select(FrameArtifactRow).where(
                    FrameArtifactRow.organization_id == organization_id,
                    FrameArtifactRow.camera_id == camera_id,
                )
            ).all()
            assert len(artifacts) == 4
    finally:
        engine.dispose()
