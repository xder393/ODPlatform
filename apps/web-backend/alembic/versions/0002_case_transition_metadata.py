"""add durable case update and transition correlation metadata

Revision ID: 0002_case_transition_metadata
Revises: 0001_p0_business_state
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op


revision = "0002_case_transition_metadata"
down_revision = "0001_p0_business_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("defect_cases") as batch:
        batch.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute("UPDATE defect_cases SET updated_at = CURRENT_TIMESTAMP")
    with op.batch_alter_table("defect_cases") as batch:
        batch.alter_column("updated_at", nullable=False)
    with op.batch_alter_table("case_transitions") as batch:
        batch.add_column(sa.Column("correlation_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("case_transitions") as batch:
        batch.drop_column("correlation_id")
    with op.batch_alter_table("defect_cases") as batch:
        batch.drop_column("updated_at")
