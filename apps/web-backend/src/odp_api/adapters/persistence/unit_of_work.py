"""One transaction for a case mutation, transition history, and audit-chain append."""

from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.repositories import (
    SqlAlchemyAuditSessionRepository,
    SqlAlchemyCaseSessionRepository,
)


class SqlAlchemyBusinessUnitOfWork:
    """Use row locks on PostgreSQL and ``BEGIN IMMEDIATE`` write serialization on SQLite."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._committed = False

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        if self._session.get_bind().dialect.name == "sqlite":
            # SQLite has no row locks. Acquiring the write reservation before
            # loading the case prevents two writers from deriving transitions
            # from the same stale status or audit head.
            self._session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        self.cases = SqlAlchemyCaseSessionRepository(self._session)
        self.audits = SqlAlchemyAuditSessionRepository(self._session)
        return self

    def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("The unit of work has not been entered.")
        self._session.commit()
        self._committed = True

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None:
        if self._session is None:
            return None
        try:
            if not self._committed:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None
        return None
