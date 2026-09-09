"""Persist ingestion ownership, generation, and sequence high-water state.

The downgrade is intended only for stopped test environments.  Production
rollback requires the coordinated procedure in the ingestion session recovery
design: stop old binaries, preserve compatible data, and use a backup or
forward repair rather than running this downgrade against live services.
"""

import sqlalchemy as sa

from alembic import op

revision = "0014_ingestion_session_leases"
down_revision = "0013_session_product_scope"
branch_labels = None
depends_on = None


_SESSION_GENERATION_CHECK = "ck_inspection_session_generation_nonnegative"
_SESSION_SEQUENCE_CHECK = "ck_inspection_session_last_reserved_sequence_nonnegative"
_ARTIFACT_GENERATION_CHECK = "ck_frame_artifact_ingestion_generation_nonnegative"


def upgrade() -> None:
    op.add_column(
        "inspection_sessions",
        sa.Column("owner_instance_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "inspection_sessions",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inspection_sessions",
        sa.Column(
            "ingestion_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "inspection_sessions",
        sa.Column(
            "last_reserved_sequence",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "frame_artifacts",
        sa.Column(
            "ingestion_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.execute(
        sa.text(
            """
            UPDATE inspection_sessions
            SET last_reserved_sequence = COALESCE((
              SELECT MAX(frame_sequence) FROM frame_artifacts
              WHERE frame_artifacts.stream_session_id = inspection_sessions.session_id
                AND frame_artifacts.organization_id = inspection_sessions.organization_id
                AND frame_artifacts.camera_id = inspection_sessions.camera_id
            ), 0)
            """
        )
    )

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("inspection_sessions") as batch:
            batch.create_check_constraint(
                _SESSION_GENERATION_CHECK,
                "ingestion_generation >= 0",
            )
            batch.create_check_constraint(
                _SESSION_SEQUENCE_CHECK,
                "last_reserved_sequence >= 0",
            )
        with op.batch_alter_table("frame_artifacts") as batch:
            batch.create_check_constraint(
                _ARTIFACT_GENERATION_CHECK,
                "ingestion_generation >= 0",
            )
    else:
        op.create_check_constraint(
            _SESSION_GENERATION_CHECK,
            "inspection_sessions",
            "ingestion_generation >= 0",
        )
        op.create_check_constraint(
            _SESSION_SEQUENCE_CHECK,
            "inspection_sessions",
            "last_reserved_sequence >= 0",
        )
        op.create_check_constraint(
            _ARTIFACT_GENERATION_CHECK,
            "frame_artifacts",
            "ingestion_generation >= 0",
        )

    op.create_index(
        "ix_inspection_sessions_recovery_candidates",
        "inspection_sessions",
        ["status", "lease_expires_at", "session_id"],
    )


def downgrade() -> None:
    # See the module docstring: this is deliberately limited to stopped test
    # environments and is not a production rollback procedure.
    op.drop_index(
        "ix_inspection_sessions_recovery_candidates",
        table_name="inspection_sessions",
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("frame_artifacts") as batch:
            batch.drop_constraint(_ARTIFACT_GENERATION_CHECK, type_="check")
            batch.drop_column("ingestion_generation")
        with op.batch_alter_table("inspection_sessions") as batch:
            batch.drop_constraint(_SESSION_SEQUENCE_CHECK, type_="check")
            batch.drop_constraint(_SESSION_GENERATION_CHECK, type_="check")
            batch.drop_column("last_reserved_sequence")
            batch.drop_column("ingestion_generation")
            batch.drop_column("lease_expires_at")
            batch.drop_column("owner_instance_id")
    else:
        op.drop_constraint(_ARTIFACT_GENERATION_CHECK, "frame_artifacts", type_="check")
        op.drop_constraint(_SESSION_SEQUENCE_CHECK, "inspection_sessions", type_="check")
        op.drop_constraint(_SESSION_GENERATION_CHECK, "inspection_sessions", type_="check")
        op.drop_column("frame_artifacts", "ingestion_generation")
        op.drop_column("inspection_sessions", "last_reserved_sequence")
        op.drop_column("inspection_sessions", "ingestion_generation")
        op.drop_column("inspection_sessions", "lease_expires_at")
        op.drop_column("inspection_sessions", "owner_instance_id")
