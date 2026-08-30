"""PostgreSQL-only concurrency coverage for fenced task execution."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import func, select, text, update

from alembic import command

WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC)]

from odp_api.adapters.persistence.inspection_effects import SqlAlchemyInspectionEffects
from odp_api.adapters.persistence.task_control import (
    SqlAlchemyTaskControlRepository,
)
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
    PublishedInferenceResultRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.inspection.effects import InspectionEffectService
from odp_api.modules.tasks.commands import (
    DeliveryOutcome,
    DeliveryRequest,
    InferenceExecutionContract,
    PublishInferenceCommand,
    WorkerDeliveryScope,
)
from odp_api.modules.tasks.models import TaskStatus
from odp_api.modules.tasks.recovery import SystemRecoveryScope
from odp_api.ports.tasks import StaleLease

pytestmark = pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL CI database",
)


def _migrate_head(database_url: str) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _create_ready_tasks(sessions, *, task_count=1):
    organization_id, camera_id, session_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    task_ids = [uuid4() for _ in range(task_count)]
    artifact_ids = [uuid4() for _ in range(task_count)]
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
        session.flush()
        session.add(
            CameraInferenceStateRow(
                organization_id=organization_id,
                camera_id=camera_id,
                running_task_id=None,
                ready_count=task_count,
                version=0,
            )
        )
        for sequence, (artifact_id, task_id) in enumerate(
            zip(artifact_ids, task_ids, strict=True), start=1
        ):
            session.add_all(
                [
                    FrameArtifactRow(
                        artifact_id=artifact_id,
                        organization_id=organization_id,
                        camera_id=camera_id,
                        stream_session_id=session_id,
                        frame_sequence=sequence,
                        captured_at=now,
                        sha256=f"{sequence:064x}",
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
                        idempotency_key=str(uuid4()),
                        status=TaskStatus.READY.value,
                        dispatch_seq=1,
                        attempt_count=0,
                        fence_token=0,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
    return organization_id, camera_id, task_ids


def _command(claim):
    contract = InferenceExecutionContract(
        "model",
        "b" * 64,
        "1",
        "cpu",
        (1, 3, 4, 4),
        "pre",
        "post",
        0.5,
        0.5,
        "hard",
        False,
        "classes",
    )
    return PublishInferenceCommand(
        claim, contract, f"{1:064x}", (), (), uuid4(), datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    )


def test_postgresql_claim_serializes_workers_for_one_camera():
    engine, sessions = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    try:
        _migrate_head(os.environ["ODP_POSTGRES_TEST_URL"])
        organization_id, camera_id, task_ids = _create_ready_tasks(sessions, task_count=2)
        barrier = Barrier(2)

        def claim(task_id, worker_id):
            barrier.wait()
            return SqlAlchemyTaskControlRepository(sessions).claim(
                task_id, organization_id, worker_id, datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(claim, task_id, f"worker-{index}")
                for index, task_id in enumerate(task_ids, start=1)
            ]
            claims = [future.result() for future in futures]

        assert sum(claim is not None for claim in claims) == 1
        with sessions() as session:
            tasks = session.scalars(
                select(InferenceTaskRow).where(
                    InferenceTaskRow.organization_id == organization_id,
                    InferenceTaskRow.camera_id == camera_id,
                    InferenceTaskRow.task_id.in_(task_ids),
                )
            ).all()
            attempts = session.scalars(
                select(InferenceAttemptRow).where(
                    InferenceAttemptRow.organization_id == organization_id,
                    InferenceAttemptRow.task_id.in_(task_ids),
                )
            ).all()
            assert sum(task.status == TaskStatus.RUNNING.value for task in tasks) == 1
            assert len(attempts) == 1
    finally:
        engine.dispose()


def test_postgresql_delivery_claim_rechecks_dispatch_after_locked_redispatch_commit():
    """A waiter must observe the dispatch generation protected by the task row lock."""

    engine, sessions = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    try:
        _migrate_head(os.environ["ODP_POSTGRES_TEST_URL"])
        organization_id, _, (task_id,) = _create_ready_tasks(sessions)
        redispatch_has_lock = Event()
        permit_redispatch_commit = Event()

        def commit_redispatch():
            with sessions.begin() as session:
                task = session.scalar(
                    select(InferenceTaskRow)
                    .where(InferenceTaskRow.task_id == task_id)
                    .with_for_update()
                )
                task.dispatch_seq = 2
                redispatch_has_lock.set()
                assert permit_redispatch_commit.wait(timeout=5)

        request = DeliveryRequest(
            stream_name="odp:inference:tasks",
            message_id="dispatch-race-1",
            event_id=uuid4(),
            event_type="vision.inference.requested.v1",
            schema_version="1",
            raw_payload=b"{}",
            organization_id=organization_id,
            task_id=task_id,
            expected_dispatch_seq=1,
        )
        repository = SqlAlchemyTaskControlRepository(sessions)
        scope = WorkerDeliveryScope("worker-race")
        with ThreadPoolExecutor(max_workers=2) as executor:
            redispatch = executor.submit(commit_redispatch)
            assert redispatch_has_lock.wait(timeout=5)
            claim = executor.submit(
                repository.accept_delivery,
                request,
                "worker-race",
                datetime.now(UTC),
                scope,
            )
            permit_redispatch_commit.set()
            redispatch.result(timeout=5)
            decision = claim.result(timeout=5)

        assert decision.outcome is DeliveryOutcome.DUPLICATE
        assert decision.claim is None
        with sessions() as session:
            task = session.get(InferenceTaskRow, task_id)
            assert (task.status, task.dispatch_seq, task.attempt_count) == (
                TaskStatus.READY.value,
                2,
                0,
            )
            assert session.scalar(
                select(func.count()).select_from(InferenceAttemptRow).where(
                    InferenceAttemptRow.task_id == task_id
                )
            ) == 0
    finally:
        engine.dispose()


def test_postgresql_renewal_keeps_fence_token_and_rejects_wrong_ownership():
    engine, sessions = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    try:
        _migrate_head(os.environ["ODP_POSTGRES_TEST_URL"])
        organization_id, _, (task_id,) = _create_ready_tasks(sessions)
        repository = SqlAlchemyTaskControlRepository(sessions)
        claim = repository.claim(task_id, organization_id, "worker", datetime.now(UTC))
        assert claim is not None

        renewed = repository.renew(claim, datetime.now(UTC))

        assert renewed is not None
        assert renewed.fence_token == claim.fence_token
        assert renewed.lease_expires_at > claim.lease_expires_at
        assert repository.renew(replace(claim, lease_owner="other"), datetime.now(UTC)) is None
        assert (
            repository.renew(replace(claim, fence_token=claim.fence_token + 1), datetime.now(UTC))
            is None
        )
    finally:
        engine.dispose()


def test_postgresql_expired_fence_cannot_publish_or_change_business_state():
    engine, sessions = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    try:
        _migrate_head(os.environ["ODP_POSTGRES_TEST_URL"])
        organization_id, camera_id, (task_id,) = _create_ready_tasks(sessions)
        repository = SqlAlchemyTaskControlRepository(sessions)
        claim = repository.claim(task_id, organization_id, "worker", datetime.now(UTC))
        assert claim is not None
        with sessions.begin() as session:
            session.execute(
                update(InferenceTaskRow)
                .where(
                    InferenceTaskRow.task_id == task_id,
                    InferenceTaskRow.organization_id == organization_id,
                )
                .values(lease_expires_at=func.now() - text("interval '1 second'"))
            )

        with pytest.raises(StaleLease):
            InspectionEffectService(SqlAlchemyInspectionEffects(sessions)).publish(
                _command(claim)
            )

        with sessions() as session:
            assert (
                session.scalars(
                    select(PublishedInferenceResultRow).where(
                        PublishedInferenceResultRow.organization_id == organization_id,
                        PublishedInferenceResultRow.task_id == task_id,
                    )
                ).all()
                == []
            )
            task = session.get(InferenceTaskRow, task_id)
            attempt = session.get(InferenceAttemptRow, claim.attempt_id)
            state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
            assert task.status == TaskStatus.RUNNING.value
            assert attempt.finished_at is None
            assert state.running_task_id == task_id
    finally:
        engine.dispose()


def test_postgresql_expired_fence_is_reclaimed_with_new_attempt():
    engine, sessions = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    try:
        _migrate_head(os.environ["ODP_POSTGRES_TEST_URL"])
        organization_id, _, (task_id,) = _create_ready_tasks(sessions)
        repository = SqlAlchemyTaskControlRepository(sessions)
        old = repository.claim(task_id, organization_id, "worker-a", datetime.now(UTC))
        assert old is not None
        with sessions.begin() as session:
            session.execute(
                update(InferenceTaskRow)
                .where(
                    InferenceTaskRow.task_id == task_id,
                    InferenceTaskRow.organization_id == organization_id,
                )
                .values(lease_expires_at=func.now() - text("interval '1 second'"))
            )
        new = repository.claim(task_id, organization_id, "worker-b", datetime.now(UTC))
        assert new is not None
        assert new.fence_token == old.fence_token + 1
        with sessions() as session:
            attempts = session.scalars(
                select(InferenceAttemptRow)
                .where(InferenceAttemptRow.task_id == task_id)
                .order_by(InferenceAttemptRow.attempt_no)
            ).all()
            assert attempts[0].outcome == "LEASE_EXPIRED"
            assert attempts[1].outcome is None
    finally:
        engine.dispose()


def test_postgresql_due_retry_scheduler_contends_without_exceeding_camera_capacity():
    engine, sessions = create_engine_and_session(os.environ["ODP_POSTGRES_TEST_URL"])
    try:
        _migrate_head(os.environ["ODP_POSTGRES_TEST_URL"])
        organization_id, camera_id, task_ids = _create_ready_tasks(sessions, task_count=2)
        with sessions.begin() as session:
            session.execute(
                update(InferenceTaskRow)
                .where(InferenceTaskRow.task_id.in_(task_ids))
                .values(
                    status=TaskStatus.RETRY_WAIT.value,
                    next_attempt_at=func.now() - text("interval '1 second'"),
                )
            )
            state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
            state.ready_count = 0
            session_id = session.scalar(
                select(InspectionSessionRow.session_id).where(
                    InspectionSessionRow.organization_id == organization_id,
                    InspectionSessionRow.camera_id == camera_id,
                )
            )
            artifact_id, ready_task_id = uuid4(), uuid4()
            session.add_all(
                [
                    FrameArtifactRow(
                        artifact_id=artifact_id,
                        organization_id=organization_id,
                        camera_id=camera_id,
                        stream_session_id=session_id,
                        frame_sequence=3,
                        captured_at=datetime.now(UTC),
                        sha256="c" * 64,
                        state="AVAILABLE",
                        lifecycle="PROCESSING",
                    ),
                    InferenceTaskRow(
                        task_id=ready_task_id,
                        organization_id=organization_id,
                        camera_id=camera_id,
                        artifact_id=artifact_id,
                        idempotency_key=str(ready_task_id),
                        status=TaskStatus.READY.value,
                        dispatch_seq=1,
                        attempt_count=0,
                        fence_token=0,
                    ),
                ]
            )
            state.ready_count = 1
        barrier = Barrier(2)
        scope = SystemRecoveryScope("postgres-concurrency")

        def release_due_retries():
            barrier.wait()
            return SqlAlchemyTaskControlRepository(sessions).release_due_retries(
                datetime.now(UTC), scope
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(release_due_retries) for _ in range(2)]
            assert sum(future.result(timeout=30) for future in results) == 1

        with sessions() as session:
            tasks = session.scalars(
                select(InferenceTaskRow).where(InferenceTaskRow.task_id.in_(task_ids))
            ).all()
            outboxes = session.scalars(
                select(OutboxEventRow).where(
                    OutboxEventRow.task_id.in_(task_ids),
                    OutboxEventRow.dispatch_seq == 2,
                    OutboxEventRow.event_type == "vision.inference.requested.v1",
                )
            ).all()
            assert sum(task.status == TaskStatus.READY.value for task in tasks) == 1
            assert sum(task.status == TaskStatus.RETRY_WAIT.value for task in tasks) == 1
            assert len(outboxes) == 1
            state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
            assert state.ready_count == 2
    finally:
        engine.dispose()
