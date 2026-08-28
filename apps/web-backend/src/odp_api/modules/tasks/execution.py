"""Application-facing fenced execution service."""

from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository


class TaskExecutionService(SqlAlchemyTaskControlRepository):
    """Expose the execution port while retaining one persistence authority."""
