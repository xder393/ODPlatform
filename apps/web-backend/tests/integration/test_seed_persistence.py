from collections.abc import Callable
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.models import (
    ActorRow,
    Base,
    DefectCaseRow,
    InspectionEventRow,
)
from odp_api.adapters.persistence.repositories import SqlAlchemyPasswordCredentialRepository
from odp_api.db import create_engine_and_session
from odp_api.seed import DEMO_ACCOUNTS, build_demo_seed, main, seed_business_data


@pytest.fixture
def session_factory(tmp_path) -> sessionmaker[Session]:
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'seed.db'}")
    Base.metadata.create_all(engine)
    return sessions


def count_rows(session_factory: sessionmaker[Session], row_type: type[object]) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(row_type)) or 0


def upgrade_sqlite_database(database_url: str) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def test_repeated_seed_does_not_duplicate_business_rows(
    session_factory: sessionmaker[Session],
) -> None:
    """Stable IDs make a restart-safe demo seed leave one copy of every row."""
    hasher: Callable[[str], str] = PasswordHasher().hash

    seed_business_data(session_factory, build_demo_seed(), hasher)
    first_actor = build_demo_seed().actors[0]
    original_hash = SqlAlchemyPasswordCredentialRepository(session_factory).password_hash(
        first_actor.actor_id
    )
    seed_business_data(session_factory, build_demo_seed(), hasher)

    assert count_rows(session_factory, ActorRow) == 3
    assert count_rows(session_factory, DefectCaseRow) == 10
    assert count_rows(session_factory, InspectionEventRow) == 10
    assert SqlAlchemyPasswordCredentialRepository(session_factory).password_hash(
        first_actor.actor_id
    ) == original_hash


def test_seed_module_runs_against_migrated_database_without_plaintext_output(
    tmp_path, monkeypatch, capsys
) -> None:
    database_url = f"sqlite:///{tmp_path / 'seeded.db'}"
    monkeypatch.setenv("ODP_DATABASE_URL", database_url)
    upgrade_sqlite_database(database_url)

    main()
    main()

    output = capsys.readouterr()
    for _email, password, _role in DEMO_ACCOUNTS:
        assert password not in output.out
        assert password not in output.err
    engine, sessions = create_engine_and_session(database_url)
    try:
        assert count_rows(sessions, ActorRow) == 3
        assert count_rows(sessions, DefectCaseRow) == 10
        assert count_rows(sessions, InspectionEventRow) == 10
    finally:
        engine.dispose()


def test_seed_module_raises_when_its_configured_database_cannot_open(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'missing' / 'seeded.db'}"
    monkeypatch.setenv("ODP_DATABASE_URL", database_url)

    with pytest.raises(OperationalError):
        main()
