"""SQLAlchemy implementations of identity persistence ports."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.models import ActorLineGrantRow, ActorRow, PasswordCredentialRow
from odp_api.modules.identity.models import Actor, Role


class SqlAlchemyActorRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, actor_id: UUID) -> Actor | None:
        with self._session_factory() as session:
            row = session.get(ActorRow, actor_id)
            return _to_actor(session, row) if row is not None else None

    def get_by_email(self, email: str) -> Actor | None:
        with self._session_factory() as session:
            row = session.scalar(select(ActorRow).where(ActorRow.email == email))
            return _to_actor(session, row) if row is not None else None


class SqlAlchemyPasswordCredentialRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def password_hash(self, actor_id: UUID) -> str | None:
        with self._session_factory() as session:
            row = session.get(PasswordCredentialRow, actor_id)
            return row.password_hash if row is not None else None


def _to_actor(session: Session, row: ActorRow) -> Actor:
    line_ids = session.scalars(
        select(ActorLineGrantRow.line_id).where(ActorLineGrantRow.actor_id == row.actor_id)
    )
    return Actor(
        actor_id=row.actor_id,
        organization_id=row.organization_id,
        role=Role(row.role),
        line_ids=frozenset(line_ids),
        email=row.email,
    )
