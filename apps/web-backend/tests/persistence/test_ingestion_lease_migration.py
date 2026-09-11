"""Migration contracts for durable ingestion ownership and sequence state."""

from __future__ import annotations

import os
import re
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InspectionSessionRow,
)
from odp_api.db import create_engine_and_session

BACKEND_DIR = Path(__file__).parents[2]
NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
NOW_VALUE = NOW.isoformat(sep=" ")


def _config(database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_session(connection, *, session_id: UUID, organization_id: UUID, camera_id: UUID, status: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO inspection_sessions (
                session_id, organization_id, camera_id, line_id, source_type,
                sanitized_uri, secret_reference, status, idempotency_key,
                heartbeat_at, error_code, error_detail, started_at, stopped_at,
                created_at, updated_at, product_category
            ) VALUES (
                :session_id, :organization_id, :camera_id, :line_id, 'TEST',
                '/safe/fixture.mp4', NULL, :status, :idempotency_key,
                NULL, NULL, NULL, :started_at, NULL,
                :created_at, :updated_at, NULL
            )
            """
        ),
        {
            "session_id": str(session_id),
            "organization_id": str(organization_id),
            "camera_id": str(camera_id),
            "line_id": str(uuid4()),
            "status": status,
            "idempotency_key": str(session_id),
            "started_at": NOW_VALUE,
            "created_at": NOW_VALUE,
            "updated_at": NOW_VALUE,
        },
    )


def _insert_artifact(
    connection,
    *,
    artifact_id: UUID,
    organization_id: UUID,
    camera_id: UUID,
    session_id: UUID,
    frame_sequence: int,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO frame_artifacts (
                artifact_id, organization_id, camera_id, stream_session_id,
                frame_sequence, captured_at, object_key, sha256, content_length,
                state, lifecycle, retention_until, error_code, error_detail,
                created_at, updated_at
            ) VALUES (
                :artifact_id, :organization_id, :camera_id, :session_id,
                :frame_sequence, :captured_at, NULL, :sha256, 1,
                'AVAILABLE', 'EVIDENCE', NULL, NULL, NULL,
                :created_at, :updated_at
            )
            """
        ),
        {
            "artifact_id": str(artifact_id),
            "organization_id": str(organization_id),
            "camera_id": str(camera_id),
            "session_id": str(session_id),
            "frame_sequence": frame_sequence,
            "captured_at": NOW_VALUE,
            "sha256": f"{artifact_id.hex:0<64}",
            "created_at": NOW_VALUE,
            "updated_at": NOW_VALUE,
        },
    )


def _exercise_migration(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "0013_session_product_scope")
    engine, _ = create_engine_and_session(database_url)
    populated_session = uuid4()
    populated_org, populated_camera = uuid4(), uuid4()
    empty_session = uuid4()
    stopped_session = uuid4()
    try:
        with engine.begin() as connection:
            _insert_session(
                connection,
                session_id=populated_session,
                organization_id=populated_org,
                camera_id=populated_camera,
                status="RUNNING",
            )
            _insert_session(
                connection,
                session_id=empty_session,
                organization_id=uuid4(),
                camera_id=uuid4(),
                status="START_REQUESTED",
            )
            stopped_org, stopped_camera = uuid4(), uuid4()
            _insert_session(
                connection,
                session_id=stopped_session,
                organization_id=stopped_org,
                camera_id=stopped_camera,
                status="STOP_REQUESTED",
            )
            _insert_artifact(
                connection,
                artifact_id=uuid4(),
                organization_id=populated_org,
                camera_id=populated_camera,
                session_id=populated_session,
                frame_sequence=2,
            )
            _insert_artifact(
                connection,
                artifact_id=uuid4(),
                organization_id=populated_org,
                camera_id=populated_camera,
                session_id=populated_session,
                frame_sequence=9,
            )
            # These rows share the session id but are outside the tenant/camera
            # scope and must not influence the recovered high-water mark.
            _insert_artifact(
                connection,
                artifact_id=uuid4(),
                organization_id=uuid4(),
                camera_id=populated_camera,
                session_id=populated_session,
                frame_sequence=100,
            )
            _insert_artifact(
                connection,
                artifact_id=uuid4(),
                organization_id=populated_org,
                camera_id=uuid4(),
                session_id=populated_session,
                frame_sequence=101,
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            rows = {
                UUID(str(row.session_id)): row
                for row in connection.execute(
                    text(
                        """
                        SELECT session_id, status, owner_instance_id,
                               lease_expires_at, ingestion_generation,
                               last_reserved_sequence
                        FROM inspection_sessions
                        """
                    )
                ).mappings()
            }
            assert rows[populated_session]["status"] == "RUNNING"
            assert rows[populated_session]["owner_instance_id"] is None
            assert rows[populated_session]["lease_expires_at"] is None
            assert rows[populated_session]["ingestion_generation"] == 0
            assert rows[populated_session]["last_reserved_sequence"] == 9

            assert rows[empty_session]["status"] == "START_REQUESTED"
            assert rows[empty_session]["ingestion_generation"] == 0
            assert rows[empty_session]["last_reserved_sequence"] == 0

            assert rows[stopped_session]["status"] == "STOP_REQUESTED"
            assert rows[stopped_session]["owner_instance_id"] is None
            assert rows[stopped_session]["lease_expires_at"] is None
            assert rows[stopped_session]["ingestion_generation"] == 0
            assert rows[stopped_session]["last_reserved_sequence"] == 0

            artifact_generations = connection.execute(
                text(
                    "SELECT ingestion_generation FROM frame_artifacts "
                    "WHERE stream_session_id = :session_id ORDER BY frame_sequence"
                ),
                {"session_id": str(populated_session)},
            ).scalars().all()
            assert artifact_generations == [0, 0, 0, 0]

            session_columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("inspection_sessions")
            }
            assert session_columns["owner_instance_id"]["nullable"] is True
            assert session_columns["lease_expires_at"]["nullable"] is True
            assert session_columns["ingestion_generation"]["nullable"] is False
            assert session_columns["last_reserved_sequence"]["nullable"] is False
            assert session_columns["ingestion_generation"]["default"] is not None
            assert session_columns["last_reserved_sequence"]["default"] is not None

            # Assert the physical dialect types, not just ORM metadata.  The
            # lease/generation values must remain 64-bit on both engines and
            # the owner/time columns must retain their dialect-specific forms.
            assert session_columns["ingestion_generation"]["type"].compile(
                dialect=connection.dialect
            ).upper() == "BIGINT"
            assert session_columns["last_reserved_sequence"]["type"].compile(
                dialect=connection.dialect
            ).upper() == "BIGINT"
            if connection.dialect.name == "postgresql":
                assert session_columns["owner_instance_id"]["type"].compile(
                    dialect=connection.dialect
                ).upper() == "UUID"
                assert session_columns["lease_expires_at"]["type"].compile(
                    dialect=connection.dialect
                ).upper() == "TIMESTAMP WITH TIME ZONE"
            else:
                assert session_columns["owner_instance_id"]["type"].compile(
                    dialect=connection.dialect
                ).upper() == "CHAR(32)"
                assert session_columns["lease_expires_at"]["type"].compile(
                    dialect=connection.dialect
                ).upper() == "DATETIME"

            artifact_columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("frame_artifacts")
            }
            assert artifact_columns["ingestion_generation"]["nullable"] is False
            assert artifact_columns["ingestion_generation"]["default"] is not None
            assert artifact_columns["ingestion_generation"]["type"].compile(
                dialect=connection.dialect
            ).upper() == "BIGINT"

            unique_constraints = {
                item["name"]: tuple(item["column_names"])
                for item in inspect(connection).get_unique_constraints(
                    "inspection_sessions"
                )
            }
            assert unique_constraints["uq_inspection_session_tenant_key"] == (
                "organization_id",
                "idempotency_key",
            )
            metadata_unique = next(
                constraint
                for constraint in InspectionSessionRow.__table__.constraints
                if constraint.name == "uq_inspection_session_tenant_key"
            )
            assert tuple(column.name for column in metadata_unique.columns) == (
                "organization_id",
                "idempotency_key",
            )

            session_indexes = {
                item["name"]: item
                for item in inspect(connection).get_indexes("inspection_sessions")
            }
            recovery_index = session_indexes["ix_inspection_sessions_recovery_candidates"]
            assert recovery_index["column_names"] == [
                "status",
                "lease_expires_at",
                "session_id",
            ]
            metadata_recovery_index = next(
                index
                for index in InspectionSessionRow.__table__.indexes
                if index.name == "ix_inspection_sessions_recovery_candidates"
            )
            assert [column.name for column in metadata_recovery_index.columns] == [
                "status",
                "lease_expires_at",
                "session_id",
            ]

            session_checks = {
                item["name"] for item in inspect(connection).get_check_constraints("inspection_sessions")
            }
            assert {
                "ck_inspection_session_generation_nonnegative",
                "ck_inspection_session_last_reserved_sequence_nonnegative",
            } <= session_checks
            artifact_checks = {
                item["name"] for item in inspect(connection).get_check_constraints("frame_artifacts")
            }
            assert "ck_frame_artifact_ingestion_generation_nonnegative" in artifact_checks
            assert {
                "ck_inspection_session_generation_nonnegative",
                "ck_inspection_session_last_reserved_sequence_nonnegative",
            } <= {
                constraint.name
                for constraint in InspectionSessionRow.__table__.constraints
                if constraint.name is not None
            }
            assert "ck_frame_artifact_ingestion_generation_nonnegative" in {
                constraint.name
                for constraint in FrameArtifactRow.__table__.constraints
                if constraint.name is not None
            }
    finally:
        engine.dispose()


def test_sqlite_ingestion_lease_migration_backfills_scoped_sequence_state(tmp_path) -> None:
    _exercise_migration(f"sqlite:///{tmp_path / 'ingestion-lease.db'}")


@pytest.fixture
def postgres_migration_url():
    database_url = os.getenv("ODP_POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("requires the dedicated ODP_POSTGRES_TEST_URL CI database")
    shared_url = make_url(database_url)
    database_name = f"odp_task1_migration_{uuid4().hex}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin_engine = create_engine(
        shared_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    isolated_url = shared_url.set(database=database_name).render_as_string(hide_password=False)
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        yield isolated_url
    finally:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_engine.dispose()


def test_postgresql_ingestion_lease_migration_backfills_scoped_sequence_state(
    postgres_migration_url,
) -> None:
    _exercise_migration(postgres_migration_url)


def test_ingestion_claim_dtos_are_frozen_and_keep_public_session_shape() -> None:
    from odp_api.ports.inspection_sessions import (
        ClaimedInspectionSession,
        IngestionClaim,
        InspectionSession,
    )

    session = InspectionSession(
        session_id=uuid4(),
        organization_id=uuid4(),
        camera_id=uuid4(),
        line_id=uuid4(),
        source_type="TEST",
        sanitized_uri="/safe/fixture.mp4",
        secret_reference=None,
        status="RUNNING",
    )
    claim = IngestionClaim(
        organization_id=session.organization_id,
        camera_id=session.camera_id,
        session_id=session.session_id,
        owner_instance_id=uuid4(),
        generation=1,
    )
    claimed = ClaimedInspectionSession(session=session, claim=claim, initial_sequence=9)

    assert claimed.session is session
    assert claimed.claim is claim
    assert claimed.initial_sequence == 9
    assert not hasattr(claim, "__dict__")
    assert not hasattr(claimed, "__dict__")
    with pytest.raises(FrozenInstanceError):
        claim.generation = 2
