import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Condition, Event, Lock
from time import monotonic
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

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


@pytest.fixture
def postgres_camera_url():
    """Give this concurrency test an empty database and an unambiguous claim."""

    shared_url = make_url(os.environ["ODP_POSTGRES_TEST_URL"])
    database_name = f"odp_camera_admission_{uuid4().hex}"
    if re.fullmatch(r"[a-z0-9_]+", database_name) is None:
        raise AssertionError("generated PostgreSQL test database name is unsafe")
    admin_engine = create_engine(
        shared_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    isolated_url = shared_url.set(database=database_name).render_as_string(
        hide_password=False
    )
    created = True
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", isolated_url.replace("%", "%%"))
        command.upgrade(config, "head")
        yield isolated_url
    finally:
        if created:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{database_name}"'
                )
        admin_engine.dispose()


class _MonotonicTestClock:
    """Give each repository operation a stable, ordered timestamp."""

    def __init__(self, origin: datetime):
        self._origin = origin
        self._lock = Lock()
        self._ticks = 0

    def __call__(self, _session):
        with self._lock:
            value = self._origin + timedelta(microseconds=self._ticks)
            self._ticks += 1
            return value


@pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL CI database",
)
def test_postgresql_camera_admission_serializes_eviction_and_keeps_two_ready_tasks(
    postgres_camera_url,
):
    database_url = postgres_camera_url
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
        repository = SqlAlchemyTaskControlRepository(
            sessions, clock=_MonotonicTestClock(now)
        )
        first = repository.reserve(_request(organization_id, camera_id, session_id, 1, claim), now)
        with pytest.raises(AdmissionRejected, match="RESERVATION_NOT_FOUND"):
            repository.complete_upload(first.reservation_id, uuid4(), "frames/1.jpg", 10, now, claim=claim)
        first_task = repository.complete_upload(first.reservation_id, organization_id, "frames/1.jpg", 10, now, claim=claim)
        second = repository.reserve(_request(organization_id, camera_id, session_id, 2, claim), now)
        second_task = repository.complete_upload(second.reservation_id, organization_id, "frames/2.jpg", 10, now, claim=claim)

        barrier = Barrier(2)
        upload_finished = Condition()
        frame_three_reserved = Event()
        frame_four_attempted_while_reserved = Event()
        completed_uploads = 0

        def admit(frame_sequence: int):
            nonlocal completed_uploads
            barrier.wait()
            deadline = monotonic() + 5
            if frame_sequence == 4 and not frame_three_reserved.wait(
                timeout=max(0, deadline - monotonic())
            ):
                raise TimeoutError("frame 3 did not reserve before frame 4")
            while True:
                try:
                    reservation = repository.reserve(
                        _request(organization_id, camera_id, session_id, frame_sequence, claim), now
                    )
                except AdmissionRejected as error:
                    if error.reason != "ADMISSION_IN_PROGRESS":
                        raise
                    if frame_sequence == 4:
                        # Keep frame 3's PENDING reservation live while this
                        # independent transaction observes the camera guard.
                        frame_four_attempted_while_reserved.set()
                    with upload_finished:
                        while completed_uploads == 0:
                            remaining = deadline - monotonic()
                            if remaining <= 0 or not upload_finished.wait(timeout=remaining):
                                raise TimeoutError(
                                    "camera admission did not become available"
                                ) from error
                    continue

                if frame_sequence == 3:
                    frame_three_reserved.set()
                    if not frame_four_attempted_while_reserved.wait(
                        timeout=max(0, deadline - monotonic())
                    ):
                        raise TimeoutError(
                            "frame 4 did not contend while frame 3 was reserved"
                        )

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
