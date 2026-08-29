"""store outbox schema versions as numeric contract values

Revision ID: 0008_numeric_outbox_schema_version
Revises: 0007_realtime_inference_control_plane
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_numeric_outbox_schema_version"
down_revision = "0007_realtime_inference_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0007 used the legacy ``v1`` spelling. Normalize existing durable rows
    # before changing the column type so deployed databases remain upgradeable.
    op.execute("UPDATE outbox_events SET schema_version = '1' WHERE schema_version = 'v1'")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("outbox_events") as batch:
            batch.alter_column(
                "schema_version",
                existing_type=sa.String(length=32),
                type_=sa.Integer(),
                existing_nullable=False,
            )
    else:
        op.alter_column(
            "outbox_events",
            "schema_version",
            existing_type=sa.String(length=32),
            type_=sa.Integer(),
            existing_nullable=False,
            postgresql_using="schema_version::integer",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("outbox_events") as batch:
            batch.alter_column(
                "schema_version",
                existing_type=sa.Integer(),
                type_=sa.String(length=32),
                existing_nullable=False,
            )
    else:
        op.alter_column(
            "outbox_events",
            "schema_version",
            existing_type=sa.Integer(),
            type_=sa.String(length=32),
            existing_nullable=False,
            postgresql_using="'v' || schema_version::text",
        )
