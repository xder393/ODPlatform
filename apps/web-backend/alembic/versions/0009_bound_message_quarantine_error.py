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
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "postgresql":
        # Existing deployments may contain legacy diagnostic text longer than
        # the new bounded contract.  Normalize it before PostgreSQL enforces
        # VARCHAR(2048), rather than making upgrade depend on an app invariant.
        op.execute("UPDATE message_quarantine SET error = LEFT(error, 2048)")
    else:
        # SQLite does not enforce VARCHAR lengths, so make the same data
        # transformation explicit before its batch table rebuild.
        op.execute("UPDATE message_quarantine SET error = SUBSTR(error, 1, 2048)")

    if dialect_name == "sqlite":
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
