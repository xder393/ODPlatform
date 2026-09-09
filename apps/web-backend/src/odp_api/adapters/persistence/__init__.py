"""SQLAlchemy-backed durable adapters.

Importing the package registers both the P0 and P1 rows on the shared
metadata.  This keeps ``Base.metadata.create_all`` and Alembic's autogenerate
view consistent during the P1B adapter migration.
"""

from . import task_models as task_models
from .outbox import SqlAlchemyOutboxRepository

__all__ = ["SqlAlchemyOutboxRepository", "task_models"]
