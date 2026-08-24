from __future__ import with_statement

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from odp_api.adapters.persistence.models import Base

config = context.config
target_metadata = Base.metadata


def _configured_database_url() -> str:
    """Resolve startup DB URL without trampling explicit programmatic Config.

    A non-default ``Config.set_main_option`` value is a caller decision and
    takes priority. Otherwise an ``ODP_DATABASE_URL`` environment value
    overrides the checked-in SQLite default for CLI and Compose startup.
    """
    configured_url = config.get_main_option("sqlalchemy.url")
    ini_default = "sqlite:////tmp/odp-quality-inspection.sqlite3"
    if configured_url and configured_url != ini_default:
        return configured_url
    return os.environ.get("ODP_DATABASE_URL", configured_url)


config.set_main_option("sqlalchemy.url", _configured_database_url())


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
