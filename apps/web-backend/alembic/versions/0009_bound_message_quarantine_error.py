"""bound durable quarantine error text

Revision ID: 0009_bound_message_quarantine_error
Revises: 0008_numeric_outbox_schema_version
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_bound_message_quarantine_error"
down_revision = "0008_numeric_outbox_schema_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("message_quarantine") as batch:
            batch.alter_column(
                "error",
                existing_type=sa.Text(),
                type_=sa.String(length=2048),
                existing_nullable=False,
            )
    else:
        op.alter_column(
            "message_quarantine",
            "error",
            existing_type=sa.Text(),
            type_=sa.String(length=2048),
            existing_nullable=False,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("message_quarantine") as batch:
            batch.alter_column(
                "error",
                existing_type=sa.String(length=2048),
                type_=sa.Text(),
                existing_nullable=False,
            )
    else:
        op.alter_column(
            "message_quarantine",
            "error",
            existing_type=sa.String(length=2048),
            type_=sa.Text(),
            existing_nullable=False,
        )
