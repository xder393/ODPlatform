"""add bounded artifact recovery and retention indexes

Revision ID: 0011_artifact_recovery_indexes
Revises: 0010_recovery_query_indexes
Create Date: 2026-09-07
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_artifact_recovery_indexes"
down_revision = "0010_recovery_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pending = sa.text("state = 'PENDING'")
    available_retention = sa.text(
        "state = 'AVAILABLE' AND lifecycle = 'EVIDENCE' AND retention_until IS NOT NULL"
    )
    op.create_index(
        "ix_frame_artifacts_pending_updated",
        "frame_artifacts",
        ["updated_at", "artifact_id"],
        postgresql_where=pending,
        sqlite_where=pending,
    )
    op.create_index(
        "ix_frame_artifacts_retention_cleanup",
        "frame_artifacts",
        ["retention_until", "artifact_id"],
        postgresql_where=available_retention,
        sqlite_where=available_retention,
    )


def downgrade() -> None:
    op.drop_index("ix_frame_artifacts_retention_cleanup", table_name="frame_artifacts")
    op.drop_index("ix_frame_artifacts_pending_updated", table_name="frame_artifacts")
