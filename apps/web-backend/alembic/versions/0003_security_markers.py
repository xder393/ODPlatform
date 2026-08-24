"""add durable reauthentication markers

Revision ID: 0003_security_markers
Revises: 0002_case_transition_metadata
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op


revision = "0003_security_markers"
down_revision = "0002_case_transition_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reauthentication_markers",
        sa.Column("actor_id", sa.Uuid(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reauthentication_markers_expires_at", "reauthentication_markers", ["expires_at"])


def downgrade() -> None:
    op.drop_table("reauthentication_markers")
