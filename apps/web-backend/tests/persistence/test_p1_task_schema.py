"""Schema contracts for the P1 realtime inference control plane."""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from odp_api.adapters.persistence.task_models import InferenceTaskRow
from odp_api.db import create_engine_and_session

BACKEND_DIR = Path(__file__).parents[2]


def test_p1_control_plane_schema_has_required_constraints(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'p1.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine, _ = create_engine_and_session(database_url)
    try:
        inspector = inspect(engine)
        expected = {
            "camera_inference_state",
            "frame_artifacts",
            "inference_tasks",
            "inference_attempts",
            "published_inference_results",
            "outbox_events",
            "message_quarantine",
            "defect_episode",
            "inspection_sessions",
        }
        assert expected <= set(inspector.get_table_names())
        assert {
            item["name"] for item in inspector.get_unique_constraints("inference_attempts")
        } >= {
            "uq_inference_attempt_task_number",
            "uq_inference_attempt_task_fence",
        }
        assert {
            item["name"] for item in inspector.get_unique_constraints("outbox_events")
        } >= {"uq_outbox_task_dispatch_event"}

        camera_checks = {
            item["sqltext"] for item in inspector.get_check_constraints("camera_inference_state")
        }
        assert any("ready_count <= 2" in check for check in camera_checks)
        assert next(
            column["nullable"]
            for column in inspector.get_columns("inference_tasks")
            if column["name"] == "artifact_id"
        ) is False
        assert next(
            column["nullable"]
            for column in inspector.get_columns("message_quarantine")
            if column["name"] == "event_id"
        ) is False
        quarantine_error = next(
            column
            for column in inspector.get_columns("message_quarantine")
            if column["name"] == "error"
        )
        assert getattr(quarantine_error["type"], "length", None) == 2048
        assert next(
            column["nullable"]
            for column in inspector.get_columns("inspection_sessions")
            if column["name"] == "line_id"
        ) is False
    finally:
        engine.dispose()


def test_inference_task_recovery_indexes_are_partial_and_cover_planner_keys(tmp_path):
    """Recovery predicates must have status-scoped indexes with stable keys."""
    database_url = f"sqlite:///{tmp_path / 'p1-indexes.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine, _ = create_engine_and_session(database_url)
    expected = {
        "ix_inference_tasks_retry_wait_next_attempt_task": (
            ["next_attempt_at", "task_id"],
            "status = 'RETRY_WAIT'",
        ),
        "ix_inference_tasks_ready_organization_camera": (
            ["organization_id", "camera_id"],
            "status = 'READY'",
        ),
    }
    try:
        inspector = inspect(engine)
        indexes = {
            item["name"]: item
            for item in inspector.get_indexes("inference_tasks")
        }
        assert set(expected) <= set(indexes)
        for name, (columns, predicate) in expected.items():
            assert indexes[name]["column_names"] == columns
            with engine.connect() as connection:
                definition = connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (name,),
                ).scalar_one()
            assert predicate in definition

        metadata_indexes = {
            index.name: index for index in InferenceTaskRow.__table__.indexes
        }
        assert set(expected) <= set(metadata_indexes)
        for name, (columns, predicate) in expected.items():
            index = metadata_indexes[name]
            assert [column.name for column in index.columns] == columns
            assert str(index.dialect_options["postgresql"]["where"]) == predicate
    finally:
        engine.dispose()
