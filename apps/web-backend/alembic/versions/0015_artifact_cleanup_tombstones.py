"""Retain paced cleanup tombstones for failed processing uploads."""

import sqlalchemy as sa

from alembic import op

revision = "0015_artifact_cleanup_tombstones"
down_revision = "0014_ingestion_session_leases"
branch_labels = None
depends_on = None


_CLEANUP_PREDICATE = sa.text(
    "lifecycle = 'PROCESSING' AND state IN ('FAILED', 'DELETED') "
    "AND error_code IN ('INGESTION_LEASE_LOST', 'STORAGE_UPLOAD_FAILED', "
    "'ADMISSION_RESERVATION_EXPIRED')"
)


def upgrade() -> None:
    op.add_column(
        "frame_artifacts",
        sa.Column("cleanup_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_frame_artifacts_cleanup_candidates",
        "frame_artifacts",
        ["cleanup_next_attempt_at", "updated_at", "artifact_id"],
        postgresql_where=_CLEANUP_PREDICATE,
        sqlite_where=_CLEANUP_PREDICATE,
    )


def downgrade() -> None:
    op.drop_index("ix_frame_artifacts_cleanup_candidates", table_name="frame_artifacts")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("frame_artifacts") as batch:
            batch.drop_column("cleanup_next_attempt_at")
    else:
        op.drop_column("frame_artifacts", "cleanup_next_attempt_at")
