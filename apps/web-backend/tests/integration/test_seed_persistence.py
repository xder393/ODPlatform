from collections.abc import Callable

import pytest
from argon2 import PasswordHasher
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.models import (
    ActorRow,
    Base,
    DefectCaseRow,
    InspectionEventRow,
)
from odp_api.adapters.persistence.repositories import SqlAlchemyPasswordCredentialRepository
from odp_api.db import create_engine_and_session
from odp_api.seed import build_demo_seed, seed_business_data


@pytest.fixture
def session_factory(tmp_path) -> sessionmaker[Session]:
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'seed.db'}")
    Base.metadata.create_all(engine)
    return sessions


def count_rows(session_factory: sessionmaker[Session], row_type: type[object]) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(row_type)) or 0


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
