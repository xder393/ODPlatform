"""Application-facing fenced execution service."""

from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository

LEASE_SECONDS = 20
RENEW_INTERVAL_SECONDS = 5
INFERENCE_TIMEOUT_SECONDS = 10


class TaskExecutionService(SqlAlchemyTaskControlRepository):
    """Expose the execution port while retaining one persistence authority."""
