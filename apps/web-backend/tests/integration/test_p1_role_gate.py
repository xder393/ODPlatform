"""Apply production grant SQL to an explicitly disposable PostgreSQL database."""

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from odp_api.database_roles import apply_runtime_grants, bootstrap_runtime_role

URL = os.getenv("ODP_ROLE_GATE_ADMIN_URL")
pytestmark = pytest.mark.skipif(not URL, reason="requires disposable ODP_ROLE_GATE_ADMIN_URL database named odp")


@pytest.fixture(scope="module")
def database():
    shared_url = make_url(URL)
    database_name = f"odp_role_gate_{uuid4().hex}"
    if re.fullmatch(r"[a-z0-9_]+", database_name) is None:
        raise AssertionError("generated PostgreSQL test database name is unsafe")
    admin_engine = create_engine(
        shared_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    isolated_url = shared_url.set(database=database_name).render_as_string(
        hide_password=False
    )
    isolated_engine = None
    created = True
    try:
        # Bootstrap the cluster-wide login roles against the configured admin
        # database, then grant those roles on this generated database only.
        bootstrap_runtime_role(URL)
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", isolated_url.replace("%", "%%"))
        command.upgrade(config, "head")
        apply_runtime_grants(isolated_url)
        isolated_engine = create_engine(isolated_url)
        yield isolated_engine
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
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


@pytest.mark.parametrize("role,table,privilege", [
    ("odp_api", "inspection_sessions", "INSERT"),
    ("odp_api", "inspection_sessions", "UPDATE"),
    ("odp_api", "inference_tasks", "SELECT"),
    ("odp_api", "inference_tasks", "INSERT"),
    ("odp_api", "frame_artifacts", "SELECT"),
    ("odp_api", "inference_attempts", "SELECT"),
    ("odp_api", "outbox_events", "INSERT"),
    ("odp_worker", "defect_cases", "INSERT"),
    ("odp_worker", "defect_cases", "UPDATE"),
    ("odp_relay", "inference_tasks", "SELECT"),
    ("odp_scheduler", "outbox_events", "INSERT"),
    ("odp_scheduler", "inference_tasks", "INSERT"),
    ("odp_scheduler", "inspection_events", "SELECT"),
])
def test_runtime_capabilities(database, role, table, privilege):
    with database.connect() as connection:
        assert connection.scalar(text(
            "SELECT has_table_privilege(:role, :table, :privilege)"
        ), {"role": role, "table": f"public.{table}", "privilege": privilege})


@pytest.mark.parametrize("role", ["odp_api", "odp_worker", "odp_relay", "odp_scheduler"])
def test_roles_cannot_rewrite_audit_history(database, role):
    with database.connect() as connection:
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not connection.scalar(text(
                "SELECT has_table_privilege(:role, 'public.audit_logs', :privilege)"
            ), {"role": role, "privilege": privilege})


@pytest.mark.parametrize("role", ["odp_worker", "odp_relay", "odp_scheduler"])
def test_background_roles_cannot_read_passwords(database, role):
    with database.connect() as connection:
        assert not connection.scalar(text(
            "SELECT has_table_privilege(:role, 'public.password_credentials', 'SELECT')"
        ), {"role": role})


@pytest.mark.parametrize("role", ["odp_api", "odp_relay", "odp_scheduler"])
def test_actual_runtime_queries_use_nonowner_login(database, role):
    from odp_api.adapters.persistence.outbox import SqlAlchemyOutboxRepository
    from odp_api.adapters.persistence.task_control import (
        SqlAlchemyTaskControlRepository,
    )
    from odp_api.modules.inspection_sessions.models import InspectionSessionCreate
    from odp_api.modules.inspection_sessions.service import InspectionSessionService
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope

    engine = create_engine(
        make_url(database.url).set(username=role, password=f"{role}_dev")
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == role
        if role == "odp_relay":
            from datetime import timedelta

            assert SqlAlchemyOutboxRepository(sessions).claim_ready(
                1, datetime.now(UTC), "role-gate", timedelta(seconds=30)
            ) == ()
        elif role == "odp_scheduler":
            RecoveryService(
                SqlAlchemyTaskControlRepository(sessions), SystemRecoveryScope("role-gate")
            ).run_once(datetime.now(UTC))
        else:
            service = InspectionSessionService(sessions)
            organization = uuid4()
            created = service.create(organization, InspectionSessionCreate(
                camera_id=uuid4(), line_id=uuid4(), source_type="RECORDED",
                source_ref="/test/video.avi",
            ), str(uuid4()))
            assert service.request_stop(organization, created.session_id).status == "STOP_REQUESTED"
    finally:
        engine.dispose()
