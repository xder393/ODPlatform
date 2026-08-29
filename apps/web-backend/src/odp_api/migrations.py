"""Apply standalone SQL migrations in filename order (idempotent).

``python -m odp_api.migrations`` connects to ``ODP_POSTGRES_URL`` and applies
every ``migrations/*.sql`` file not yet recorded in the ``schema_migrations``
table. psycopg is imported lazily so local tests and the in-memory runtime
never load the driver.

The Compose stack runs this module before starting the API; Alembic can
replace it once the schema grows beyond a few standalone scripts.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

logger = logging.getLogger(__name__)


def applied_migrations(postgres_url: str) -> list[str]:
    """Return the migration filenames already recorded in the database."""
    import psycopg  # deferred optional runtime dependency

    with psycopg.connect(postgres_url) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   filename TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT filename FROM schema_migrations ORDER BY filename")
            return [row[0] for row in cursor.fetchall()]


def apply_migrations(
    postgres_url: str, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply every pending migration in filename order; return the new names."""
    import psycopg  # deferred optional runtime dependency

    applied = applied_migrations(postgres_url)
    pending = sorted(
        path.name for path in migrations_dir.glob("*.sql") if path.name not in applied
    )
    for filename in pending:
        sql = (migrations_dir / filename).read_text(encoding="utf-8")
        with psycopg.connect(postgres_url) as connection, connection.cursor() as cursor:
            cursor.execute(sql)
            cursor.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)",
                (filename,),
            )
        logger.info("Applied migration %s", filename)
    return pending


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    postgres_url = os.environ.get("ODP_POSTGRES_URL")
    if not postgres_url:
        raise SystemExit("ODP_POSTGRES_URL is not set.")
    new_migrations = apply_migrations(postgres_url)
    print(f"Applied {len(new_migrations)} migration(s): {new_migrations}")


if __name__ == "__main__":
    main()
