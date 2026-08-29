import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from odp_api.db import create_engine_and_session

BACKEND_DIR = Path(__file__).parents[2]


def test_alembic_cli_uses_odp_database_url_for_a_real_sqlite_upgrade(tmp_path) -> None:
    """The startup CLI must migrate the database supplied in ODP_DATABASE_URL."""
    database_url = f"sqlite:///{tmp_path / 'configured-runtime.db'}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "ODP_DATABASE_URL": database_url},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    engine, _ = create_engine_and_session(database_url)
    try:
        assert "actors" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_programmatic_alembic_url_has_priority_over_odp_environment(
    tmp_path, monkeypatch
) -> None:
    """A caller-set Config URL is intentional and wins over ambient environment."""
    configured_url = f"sqlite:///{tmp_path / 'configured.db'}"
    environment_url = f"sqlite:///{tmp_path / 'environment.db'}"
    monkeypatch.setenv("ODP_DATABASE_URL", environment_url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", configured_url)

    command.upgrade(config, "head")

    configured_engine, _ = create_engine_and_session(configured_url)
    environment_engine, _ = create_engine_and_session(environment_url)
    try:
        assert "actors" in inspect(configured_engine).get_table_names()
        assert "actors" not in inspect(environment_engine).get_table_names()
    finally:
        configured_engine.dispose()
        environment_engine.dispose()


def test_alembic_cli_renders_postgresql_ddl_and_widens_revision_storage() -> None:
    """PostgreSQL startup SQL must use PostgreSQL types and fit long revisions."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND_DIR,
        env={
            **os.environ,
            "ODP_DATABASE_URL": "postgresql+psycopg://odp:odp@localhost:5432/odp",
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "actor_id UUID NOT NULL" in result.stdout
    assert (
        "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)"
        in result.stdout
    )


@pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL CI database",
)
def test_postgresql_alembic_head_stores_long_revisions() -> None:
    """CI proves 0004 can be stamped after the ledger is widened to 64 chars."""
    database_url = os.environ["ODP_POSTGRES_TEST_URL"]
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine, _ = create_engine_and_session(database_url)
    try:
        with engine.connect() as connection:
            length = connection.scalar(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name = 'alembic_version' AND column_name = 'version_num'"
                )
            )
        assert length == 64
    finally:
        engine.dispose()
