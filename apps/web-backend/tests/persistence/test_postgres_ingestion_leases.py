"""Real PostgreSQL concurrency coverage for ingestion ownership leases."""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from odp_api.adapters.persistence.inspection_sessions import (
    SqlAlchemyInspectionSessionRepository,
)
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.ingestion.reconciler import PendingArtifact
from odp_api.ports.storage import ObjectMetadata
from odp_api.ports.tasks import AdmissionRejected, AdmissionRequest

pytestmark = pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL database",
)


def _migrate_head(database_url: str) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.fixture(scope="module")
def postgres_url():
    shared_url = make_url(os.environ["ODP_POSTGRES_TEST_URL"])
    database_name = f"odp_task2_ingestion_{uuid4().hex}"
    if re.fullmatch(r"[a-z0-9_]+", database_name) is None:
        raise AssertionError("generated PostgreSQL test database name is unsafe")
    admin_engine = create_engine(
        shared_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    isolated_url = shared_url.set(database=database_name).render_as_string(
        hide_password=False
    )
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        created = True
        _migrate_head(isolated_url)
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


def _seed(sessions, *, status="START_REQUESTED", lease_expires_at=None):
    now = datetime.now(UTC)
    session_id, organization_id, camera_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            InspectionSessionRow(
                session_id=session_id,
                organization_id=organization_id,
                camera_id=camera_id,
                line_id=uuid4(),
                source_type="TEST",
                sanitized_uri="rtsp://test.invalid/camera",
                status=status,
                owner_instance_id=None,
                lease_expires_at=lease_expires_at,
                idempotency_key=str(session_id),
                started_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return session_id


def _wait_for_pg_lock(observer_engine, application_name: str) -> None:
    """Wait for a named promotion connection to reach PostgreSQL lock wait."""

    deadline = monotonic() + 5
    while monotonic() < deadline:
        with observer_engine.connect() as observer:
            waiting = observer.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_stat_activity "
                    "WHERE application_name = :application_name "
                    "AND wait_event_type = 'Lock'"
                    ")"
                ),
                {"application_name": application_name},
            )
        if waiting:
            return
        # An Event wait keeps this condition-driven instead of using sleep as
        # proof; the lock waiter itself is the acceptance fact.
        Event().wait(0.01)
    raise AssertionError(f"{application_name} did not reach a PostgreSQL lock wait")


def test_two_independent_postgresql_factories_claim_one_session(
    postgres_url,
):
    first_engine, first_sessions = create_engine_and_session(postgres_url)
    second_engine, second_sessions = create_engine_and_session(postgres_url)
    session_id = _seed(first_sessions)
    repositories = [
        SqlAlchemyInspectionSessionRepository(first_sessions),
        SqlAlchemyInspectionSessionRepository(second_sessions),
    ]
    barrier = Barrier(2)

    def claim(repository):
        barrier.wait()
        return repository.claim_available("same-process-name", uuid4(), 1)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, repositories))
        claimed = [item for result in results for item in result]
        assert len(claimed) == 1
        assert claimed[0].session.session_id == session_id
        assert claimed[0].claim.generation == 1
    finally:
        second_engine.dispose()
        first_engine.dispose()


def test_postgresql_expired_session_takeover_has_one_generation_winner(postgres_url):
    first_engine, first_sessions = create_engine_and_session(postgres_url)
    second_engine, second_sessions = create_engine_and_session(postgres_url)
    session_id = _seed(
        first_sessions,
        status="RUNNING",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    previous_owner = uuid4()
    with first_sessions.begin() as database:
        row = database.get(InspectionSessionRow, session_id)
        row.owner_instance_id = previous_owner
        row.ingestion_generation = 7
        row.last_reserved_sequence = 41

    repositories = [
        SqlAlchemyInspectionSessionRepository(first_sessions),
        SqlAlchemyInspectionSessionRepository(second_sessions),
    ]
    barrier = Barrier(2)

    def claim(repository):
        barrier.wait()
        return repository.claim_available("recovery-race", uuid4(), 1)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, repositories))
        claimed = [item for result in results for item in result]
        assert len(claimed) == 1
        winner = claimed[0]
        assert winner.session.session_id == session_id
        assert winner.claim.generation == 8

        with first_sessions() as database:
            persisted = database.get(InspectionSessionRow, session_id)
            assert persisted.status == "RUNNING"
            assert persisted.owner_instance_id == winner.claim.owner_instance_id
            assert persisted.ingestion_generation == 8
            assert persisted.last_reserved_sequence == 41
    finally:
        second_engine.dispose()
        first_engine.dispose()


def test_postgresql_stop_request_beats_claim_while_row_lock_is_held(postgres_url):
    holder_engine, holder_sessions = create_engine_and_session(postgres_url)
    contender_engine, contender_sessions = create_engine_and_session(postgres_url)
    session_id = _seed(holder_sessions)
    repository = SqlAlchemyInspectionSessionRepository(contender_sessions)
    started = Event()

    def claim():
        started.set()
        return repository.claim_available("stop-race", uuid4(), 1)

    try:
        with holder_sessions() as holder, holder.begin():
            row = holder.execute(
                select(InspectionSessionRow)
                .where(InspectionSessionRow.session_id == session_id)
                .with_for_update()
            ).scalar_one()
            row.status = "STOP_REQUESTED"
            row.updated_at = datetime.now(UTC)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(claim)
                assert started.wait(timeout=2)
                # SKIP LOCKED must observe the stop transaction's row lock
                # as unavailable and must not promote this session.
                assert future.result(timeout=5) == []

        assert repository.finalize_expired_stops(1) == 1
        with holder_sessions() as database:
            persisted = database.get(InspectionSessionRow, session_id)
            assert (persisted.status, persisted.owner_instance_id) == ("STOPPED", None)
            assert persisted.ingestion_generation == 0
    finally:
        contender_engine.dispose()
        holder_engine.dispose()


@pytest.mark.parametrize("promotion", ["complete_upload", "reconcile_pending_artifact"])
def test_postgresql_stop_before_artifact_promotion_fences_ready_and_outbox(
    postgres_url, promotion
):
    setup_engine, setup_sessions = create_engine_and_session(postgres_url)
    stop_engine, stop_sessions = create_engine_and_session(postgres_url)
    application_name = f"ingestion-stop-promotion-{uuid4().hex}"
    promotion_engine = create_engine(
        postgres_url,
        connect_args={"application_name": application_name},
    )
    promotion_sessions = sessionmaker(bind=promotion_engine, expire_on_commit=False)
    observer_engine = create_engine(postgres_url)
    observer_sessions = sessionmaker(bind=observer_engine, expire_on_commit=False)
    session_id = _seed(setup_sessions)
    owner_instance_id = uuid4()
    claim = SqlAlchemyInspectionSessionRepository(setup_sessions).claim_available(
        "ingestor-promotion", owner_instance_id, 1
    )[0]
    now = datetime.now(UTC)
    control = SqlAlchemyTaskControlRepository(setup_sessions)
    reservation = control.reserve(
        AdmissionRequest(
            organization_id=claim.claim.organization_id,
            camera_id=claim.claim.camera_id,
            stream_session_id=session_id,
            frame_sequence=1,
            captured_at=now,
            content_sha256="a" * 64,
            correlation_id=uuid4(),
            claim=claim.claim,
        ),
        now,
    )
    candidate = PendingArtifact(
        artifact_id=reservation.artifact_id,
        organization_id=reservation.organization_id,
        object_key=(
            f"organizations/{reservation.organization_id}/artifacts/"
            f"{reservation.artifact_id}"
        ),
        sha256=reservation.content_sha256,
        content_length=None,
    )
    started = Event()

    def promote():
        started.set()
        repository = SqlAlchemyTaskControlRepository(promotion_sessions)
        if promotion == "complete_upload":
            try:
                repository.complete_upload(
                    reservation.reservation_id,
                    reservation.organization_id,
                    candidate.object_key,
                    7,
                    datetime.now(UTC),
                    claim=claim.claim,
                )
            except AdmissionRejected as error:
                return error.reason
            raise AssertionError("complete_upload unexpectedly promoted a stopped claim")
        return repository.reconcile_pending_artifact(
            candidate,
            ObjectMetadata(candidate.object_key, 7, candidate.sha256),
            datetime.now(UTC),
        )

    try:
        with stop_sessions() as holder:
            holder.begin()
            locked = holder.execute(
                select(InspectionSessionRow)
                .where(InspectionSessionRow.session_id == session_id)
                .with_for_update()
            ).scalar_one()
            locked.status = "STOP_REQUESTED"
            locked.updated_at = datetime.now(UTC)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(promote)
                assert started.wait(timeout=2)
                _wait_for_pg_lock(observer_engine, application_name)
                holder.commit()
                outcome = future.result(timeout=5)

        if promotion == "complete_upload":
            assert outcome == "INGESTION_LEASE_LOST"
        else:
            assert outcome is False

        with observer_sessions() as observer:
            persisted_session = observer.execute(
                select(InspectionSessionRow).where(
                    InspectionSessionRow.session_id == session_id
                )
            ).scalar_one()
            persisted_artifact = observer.execute(
                select(FrameArtifactRow).where(
                    FrameArtifactRow.artifact_id == reservation.artifact_id
                )
            ).scalar_one()
            persisted_state = observer.execute(
                select(CameraInferenceStateRow).where(
                    CameraInferenceStateRow.organization_id == reservation.organization_id,
                    CameraInferenceStateRow.camera_id == reservation.camera_id,
                )
            ).scalar_one()
            assert persisted_session.status == "STOP_REQUESTED"
            assert observer.scalar(
                select(InferenceTaskRow.task_id).where(
                    InferenceTaskRow.artifact_id == reservation.artifact_id,
                )
            ) is None
            assert observer.scalar(
                select(OutboxEventRow.outbox_id).where(
                    OutboxEventRow.organization_id == reservation.organization_id,
                )
            ) is None
            if promotion == "complete_upload":
                assert persisted_artifact.state == "PENDING"
                assert persisted_artifact.object_key is None
                assert persisted_state.reservation_id == reservation.reservation_id
            else:
                assert persisted_artifact.state == "FAILED"
                assert persisted_artifact.error_code == "INGESTION_LEASE_LOST"
                assert persisted_state.reservation_id is None
    finally:
        observer_engine.dispose()
        promotion_engine.dispose()
        stop_engine.dispose()
        setup_engine.dispose()


def test_postgresql_stop_and_claim_promotion_race_has_terminal_persisted_outcome(
    postgres_url,
):
    claim_engine, claim_sessions = create_engine_and_session(postgres_url)
    stop_engine, stop_sessions = create_engine_and_session(postgres_url)
    session_id = _seed(claim_sessions)
    repository = SqlAlchemyInspectionSessionRepository(claim_sessions)
    barrier = Barrier(2)
    instance_id = uuid4()

    def claim():
        barrier.wait()
        return repository.claim_available("promotion-race", instance_id, 1)

    def request_stop():
        barrier.wait()
        with stop_sessions.begin() as database:
            row = database.execute(
                select(InspectionSessionRow)
                .where(InspectionSessionRow.session_id == session_id)
                .with_for_update()
            ).scalar_one()
            row.status = "STOP_REQUESTED"
            row.updated_at = datetime.now(UTC)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claim_future = executor.submit(claim)
            stop_future = executor.submit(request_stop)
            claimed = claim_future.result(timeout=5)
            stop_future.result(timeout=5)

        with claim_sessions() as database:
            raced = database.get(InspectionSessionRow, session_id)
            assert raced.ingestion_generation in {0, 1}
            if claimed:
                assert len(claimed) == 1
                assert raced.owner_instance_id == instance_id
                assert raced.status == "STOP_REQUESTED"
                assert repository.finish_stop(claimed[0].claim)
            else:
                assert raced.status == "STOP_REQUESTED"
                assert raced.owner_instance_id is None
                assert repository.finalize_expired_stops(1) == 1

        with stop_sessions() as database:
            persisted = database.get(InspectionSessionRow, session_id)
            assert persisted.status == "STOPPED"
            assert persisted.owner_instance_id is None
            assert persisted.lease_expires_at is None
            assert persisted.ingestion_generation in {0, 1}
    finally:
        stop_engine.dispose()
        claim_engine.dispose()


def test_postgresql_renew_rechecks_clock_after_waiting_for_row_lock(postgres_url):
    holder_engine, holder_sessions = create_engine_and_session(postgres_url)
    waiter_engine = create_engine(
        postgres_url,
        connect_args={"application_name": "ingestion-lease-renew-waiter"},
    )
    waiter_sessions = sessionmaker(bind=waiter_engine, expire_on_commit=False)
    observer_engine = create_engine(postgres_url)
    session_id = _seed(holder_sessions)
    repository = SqlAlchemyInspectionSessionRepository(
        holder_sessions,
        lease_duration=timedelta(milliseconds=250),
    )
    claim = repository.claim_available("ingestor", uuid4(), 1)[0].claim
    started = Event()

    def renew():
        started.set()
        return SqlAlchemyInspectionSessionRepository(waiter_sessions).renew(claim)

    try:
        with holder_sessions() as holder:
            holder.begin()
            holder.execute(
                select(InspectionSessionRow)
                .where(InspectionSessionRow.session_id == session_id)
                .with_for_update()
            ).scalar_one()
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(renew)
                assert started.wait(timeout=2)
                deadline = monotonic() + 2
                blocked = False
                while monotonic() < deadline:
                    with observer_engine.connect() as observer:
                        blocked = observer.scalar(
                            text(
                                "SELECT EXISTS ("
                                "SELECT 1 FROM pg_stat_activity "
                                "WHERE application_name = 'ingestion-lease-renew-waiter' "
                                "AND wait_event_type = 'Lock'"
                                ")"
                            )
                        )
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked, "renew did not reach a PostgreSQL lock wait"
                sleep(0.35)
                holder.commit()
                assert future.result(timeout=5) is False
    finally:
        observer_engine.dispose()
        waiter_engine.dispose()
        holder_engine.dispose()
