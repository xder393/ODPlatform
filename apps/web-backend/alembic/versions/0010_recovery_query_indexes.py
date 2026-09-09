"""add status-scoped indexes for bounded recovery scans

Revision ID: 0010_recovery_query_indexes
Revises: 0009_bound_message_quarantine_error
Create Date: 2026-09-02
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_recovery_query_indexes"
down_revision = "0009_bound_message_quarantine_error"
branch_labels = None
depends_on = None


_RETRY_WAIT_PREDICATE = sa.text("status = 'RETRY_WAIT'")
_READY_PREDICATE = sa.text("status = 'READY'")


def upgrade() -> None:
    op.create_index(
        "ix_inference_tasks_retry_wait_next_attempt_task",
        "inference_tasks",
        ["next_attempt_at", "task_id"],
        postgresql_where=_RETRY_WAIT_PREDICATE,
        sqlite_where=_RETRY_WAIT_PREDICATE,
    )
    op.create_index(
        "ix_inference_tasks_ready_organization_camera",
        "inference_tasks",
        ["organization_id", "camera_id"],
        postgresql_where=_READY_PREDICATE,
        sqlite_where=_READY_PREDICATE,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inference_tasks_ready_organization_camera",
        table_name="inference_tasks",
    )
    op.drop_index(
        "ix_inference_tasks_retry_wait_next_attempt_task",
        table_name="inference_tasks",
    )
