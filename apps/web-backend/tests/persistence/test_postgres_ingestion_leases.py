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
from odp_api.adapters.persistence.task_models import InspectionSessionRow
from odp_api.db import create_engine_and_session

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
