"""add durable cursor-based inspection alert feed

Revision ID: 0004_durable_inspection_alert_feed
Revises: 0003_security_markers
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_durable_inspection_alert_feed"
down_revision = "0003_security_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic creates this ledger as VARCHAR(32), but this revision ID is
    # longer. Widen before Alembic stamps 0004, inside the same transaction.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")
    op.create_table(
        "inspection_alerts",
        sa.Column("cursor", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_inspection_alerts_organization_id", "inspection_alerts", ["organization_id"])
    op.create_index("ix_inspection_alerts_line_id", "inspection_alerts", ["line_id"])


def downgrade() -> None:
    op.drop_table("inspection_alerts")
    # Deliberately retain VARCHAR(64): shrinking a migration ledger can
    # truncate stamps from another compatible branch during a downgrade.
