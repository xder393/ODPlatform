"""SQLAlchemy engine and session construction shared by runtime and migrations."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_engine_and_session(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    """Create a cross-database engine and non-expiring transactional sessions."""
    engine = create_engine(database_url)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)
