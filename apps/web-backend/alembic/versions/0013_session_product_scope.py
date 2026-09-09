"""Snapshot product scope on inspection sessions without guessing historical data."""

import sqlalchemy as sa

from alembic import op

revision = "0013_session_product_scope"
down_revision = "0012_ingestor_session_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inspection_sessions", sa.Column("product_category", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("inspection_sessions", "product_category")
