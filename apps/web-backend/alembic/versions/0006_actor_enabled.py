"""add durable actor enablement

Revision ID: 0006_actor_enabled
Revises: 0005_alert_event_tenant_uniqueness
"""

import sqlalchemy as sa
from alembic import op


revision = "0006_actor_enabled"
down_revision = "0005_alert_event_tenant_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "actors",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("actors", "enabled")
