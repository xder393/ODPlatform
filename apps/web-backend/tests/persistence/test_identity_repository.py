from argon2 import PasswordHasher

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.repositories import (
    SqlAlchemyActorRepository,
    SqlAlchemyPasswordCredentialRepository,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.identity.models import Role
from odp_api.seed import build_demo_seed, seed_business_data


def test_sqlite_actor_and_credential_survive_new_session(tmp_path) -> None:
    """Actor and credential repositories read durable seed rows after restart."""
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine)
    seed_business_data(sessions, build_demo_seed(), PasswordHasher().hash)

    actor = SqlAlchemyActorRepository(sessions).get_by_email("inspector@example.test")

    assert actor is not None
    assert actor.role is Role.INSPECTOR
    assert SqlAlchemyPasswordCredentialRepository(sessions).password_hash(
        actor.actor_id
    ).startswith("$argon2id$")
