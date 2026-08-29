"""Apply the narrowly scoped PostgreSQL runtime privileges after migrations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

_REPOSITORY_ROOT = Path(__file__).parents[4]
_BOOTSTRAP_SQL = _REPOSITORY_ROOT / "deploy" / "postgres" / "001-create-app-role.sql"
_GRANT_SQL = _REPOSITORY_ROOT / "deploy" / "postgres" / "apply-runtime-grants.sql"


def _postgres_url(database_url: str) -> str:
    """Convert SQLAlchemy's psycopg URL spelling to libpq's spelling."""
    return (
        make_url(database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def bootstrap_sql() -> str:
    return _BOOTSTRAP_SQL.read_text()


def grant_sql() -> str:
    return _GRANT_SQL.read_text()


def _apply(sql: str, database_url: str) -> None:
    with psycopg.connect(_postgres_url(database_url), autocommit=True) as connection:
        connection.execute(sql)


def bootstrap_runtime_role(database_url: str | None = None) -> None:
    _apply(bootstrap_sql(), database_url or os.environ["ODP_MIGRATOR_DATABASE_URL"])


def apply_runtime_grants(database_url: str | None = None) -> None:
    _apply(grant_sql(), database_url or os.environ["ODP_MIGRATOR_DATABASE_URL"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--bootstrap-only", action="store_true")
    arguments = parser.parse_args()
    if arguments.bootstrap and arguments.bootstrap_only:
        parser.error("--bootstrap and --bootstrap-only cannot be combined")
    if arguments.bootstrap_only:
        bootstrap_runtime_role()
        return
    if arguments.bootstrap:
        bootstrap_runtime_role()
    apply_runtime_grants()


if __name__ == "__main__":
    main()
