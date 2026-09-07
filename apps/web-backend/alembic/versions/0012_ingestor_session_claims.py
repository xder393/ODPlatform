"""persist the process owner of a claimed inspection session

Revision ID: 0012_ingestor_session_claims
Revises: 0011_artifact_recovery_indexes
Create Date: 2026-09-07
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_ingestor_session_claims"
down_revision = "0011_artifact_recovery_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inspection_sessions",
        sa.Column("ingestor_process_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_inspection_sessions_ingestor_process_id",
        "inspection_sessions",
        ["ingestor_process_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inspection_sessions_ingestor_process_id",
        table_name="inspection_sessions",
    )
    op.drop_column("inspection_sessions", "ingestor_process_id")
