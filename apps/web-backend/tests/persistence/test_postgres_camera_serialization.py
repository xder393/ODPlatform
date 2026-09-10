import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Condition
from time import monotonic
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import select

from alembic import command

WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC)]

from odp_api.adapters.persistence.inspection_sessions import (
    SqlAlchemyInspectionSessionRepository,
)
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
)
from odp_api.db import create_engine_and_session
from odp_api.ports.tasks import AdmissionRejected, AdmissionRequest


def _request(organization_id, camera_id, session_id, frame_sequence, claim):
    return AdmissionRequest(
        organization_id=organization_id,
        camera_id=camera_id,
        stream_session_id=session_id,
        frame_sequence=frame_sequence,
        captured_at=datetime(2026, 8, 25, 12, 0, frame_sequence, tzinfo=UTC),
        content_sha256=f"{frame_sequence:064x}",
        correlation_id=uuid4(),
        claim=claim,
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
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        with sessions.begin() as session:
            session.add(
                InspectionSessionRow(
                    session_id=session_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    line_id=uuid4(),
                    source_type="TEST",
                    sanitized_uri="rtsp://test.invalid/camera",
                    status="START_REQUESTED",
                    idempotency_key=str(uuid4()),
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

        ownership = SqlAlchemyInspectionSessionRepository(
            sessions, clock=lambda _session: now
        )
        claim = ownership.claim_available("test-ingestor", uuid4(), 1)[0].claim
        repository = SqlAlchemyTaskControlRepository(sessions, clock=lambda _session: now)
        first = repository.reserve(_request(organization_id, camera_id, session_id, 1, claim), now)
        with pytest.raises(AdmissionRejected, match="RESERVATION_NOT_FOUND"):
            repository.complete_upload(first.reservation_id, uuid4(), "frames/1.jpg", 10, now, claim=claim)
        first_task = repository.complete_upload(first.reservation_id, organization_id, "frames/1.jpg", 10, now, claim=claim)
        second = repository.reserve(_request(organization_id, camera_id, session_id, 2, claim), now)
        second_task = repository.complete_upload(second.reservation_id, organization_id, "frames/2.jpg", 10, now, claim=claim)

        barrier = Barrier(2)
        upload_finished = Condition()
        completed_uploads = 0

        def admit(frame_sequence: int):
            nonlocal completed_uploads
            barrier.wait()
            deadline = monotonic() + 5
            while True:
                try:
                    reservation = repository.reserve(
                        _request(organization_id, camera_id, session_id, frame_sequence, claim), now
                    )
                except AdmissionRejected as error:
                    if error.reason != "ADMISSION_IN_PROGRESS":
                        raise
                    with upload_finished:
                        while completed_uploads == 0:
                            remaining = deadline - monotonic()
                            if remaining <= 0 or not upload_finished.wait(timeout=remaining):
                                raise TimeoutError(
                                    "camera admission did not become available"
                                ) from error
                    continue

                task = repository.complete_upload(
                    reservation.reservation_id,
                    organization_id,
                    f"frames/{frame_sequence}.jpg",
                    10,
                    now,
                    claim=claim,
                )
                with upload_finished:
                    completed_uploads += 1
                    upload_finished.notify_all()
                return task

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
