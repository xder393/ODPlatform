"""scope alert event idempotency by tenant

Revision ID: 0005_alert_event_tenant_uniqueness
Revises: 0004_durable_inspection_alert_feed
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_alert_event_tenant_uniqueness"
down_revision = "0004_durable_inspection_alert_feed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.create_table(
            "inspection_alerts_new",
            sa.Column("cursor", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Uuid(), nullable=False),
            sa.Column("organization_id", sa.Uuid(), nullable=False),
            sa.Column("line_id", sa.Uuid()), sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "event_id", name="uq_inspection_alerts_org_event"),
        )
        op.execute("INSERT INTO inspection_alerts_new SELECT cursor, event_id, organization_id, line_id, payload, created_at FROM inspection_alerts")
        op.drop_table("inspection_alerts")
        op.rename_table("inspection_alerts_new", "inspection_alerts")
        op.create_index("ix_inspection_alerts_organization_id", "inspection_alerts", ["organization_id"])
        op.create_index("ix_inspection_alerts_line_id", "inspection_alerts", ["line_id"])
        return
    inspector = sa.inspect(op.get_bind())
    old_unique = next(
        constraint["name"] for constraint in inspector.get_unique_constraints("inspection_alerts")
        if constraint.get("column_names") == ["event_id"]
    )
    with op.batch_alter_table("inspection_alerts") as batch:
        batch.drop_constraint(old_unique, type_="unique")
        batch.create_unique_constraint("uq_inspection_alerts_org_event", ["organization_id", "event_id"])


def downgrade() -> None:
    with op.batch_alter_table("inspection_alerts") as batch:
        batch.drop_constraint("uq_inspection_alerts_org_event", type_="unique")
        batch.create_unique_constraint("uq_inspection_alerts_event_id", ["event_id"])
