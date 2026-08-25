"""add the PostgreSQL realtime inference control plane

Revision ID: 0007_realtime_inference_control_plane
Revises: 0006_actor_enabled
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_realtime_inference_control_plane"
down_revision = "0006_actor_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "camera_inference_state",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("running_task_id", sa.Uuid(), nullable=True),
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("reservation_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("organization_id", "camera_id"),
        sa.UniqueConstraint(
            "organization_id",
            "camera_id",
            name="uq_camera_inference_state_tenant_camera",
        ),
        sa.CheckConstraint("ready_count >= 0", name="ck_camera_state_ready_nonnegative"),
        sa.CheckConstraint("version >= 0", name="ck_camera_state_version_nonnegative"),
    )

    op.create_table(
        "inspection_sessions",
        sa.Column("session_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("sanitized_uri", sa.String(2048), nullable=False),
        sa.Column("secret_reference", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_inspection_session_tenant_key",
        ),
    )
    op.create_index(
        "ix_inspection_sessions_organization_id",
        "inspection_sessions",
        ["organization_id"],
    )
    op.create_index("ix_inspection_sessions_camera_id", "inspection_sessions", ["camera_id"])
    op.create_index("ix_inspection_sessions_line_id", "inspection_sessions", ["line_id"])

    op.create_table(
        "frame_artifacts",
        sa.Column("artifact_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stream_session_id",
            sa.Uuid(),
            sa.ForeignKey("inspection_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("frame_sequence", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_length", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("lifecycle", sa.String(32), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "organization_id",
            "camera_id",
            "stream_session_id",
            "frame_sequence",
            name="uq_frame_artifact_tenant_camera_session_sequence",
        ),
        sa.CheckConstraint("frame_sequence >= 1", name="ck_frame_artifact_sequence_positive"),
        sa.CheckConstraint(
            "content_length IS NULL OR content_length >= 0",
            name="ck_frame_artifact_content_length_nonnegative",
        ),
    )
    op.create_index("ix_frame_artifacts_organization_id", "frame_artifacts", ["organization_id"])
    op.create_index("ix_frame_artifacts_camera_id", "frame_artifacts", ["camera_id"])
    op.create_index("ix_frame_artifacts_stream_session_id", "frame_artifacts", ["stream_session_id"])

    op.create_table(
        "inference_tasks",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column(
            "artifact_id",
            sa.Uuid(),
            sa.ForeignKey("frame_artifacts.artifact_id"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("dispatch_seq", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        sa.Column("fence_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_inference_task_tenant_key"),
        sa.CheckConstraint("dispatch_seq >= 1", name="ck_inference_task_dispatch_positive"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_inference_task_attempt_nonnegative"),
        sa.CheckConstraint("fence_token >= 0", name="ck_inference_task_fence_nonnegative"),
    )
    op.create_index("ix_inference_tasks_organization_id", "inference_tasks", ["organization_id"])
    op.create_index("ix_inference_tasks_camera_id", "inference_tasks", ["camera_id"])

    op.create_table(
        "inference_attempts",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("inference_tasks.task_id"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "attempt_no", name="uq_inference_attempt_task_number"),
        sa.UniqueConstraint("task_id", "fence_token", name="uq_inference_attempt_task_fence"),
        sa.CheckConstraint("attempt_no >= 1", name="ck_inference_attempt_number_positive"),
        sa.CheckConstraint("fence_token >= 1", name="ck_inference_attempt_fence_positive"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_inference_attempt_duration_nonnegative",
        ),
    )
    op.create_index("ix_inference_attempts_task_id", "inference_attempts", ["task_id"])
    op.create_index(
        "ix_inference_attempts_organization_id", "inference_attempts", ["organization_id"]
    )

    op.create_table(
        "published_inference_results",
        sa.Column("result_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("inference_tasks.task_id"), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("inference_attempts.attempt_id"), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), sa.ForeignKey("frame_artifacts.artifact_id"), nullable=False),
        sa.Column("model_release", sa.String(255), nullable=False),
        sa.Column("model_sha256", sa.String(64), nullable=False),
        sa.Column("onnxruntime_version", sa.String(64), nullable=False),
        sa.Column("execution_provider", sa.String(128), nullable=False),
        sa.Column("actual_input_shape", sa.JSON(), nullable=False),
        sa.Column("preprocessing_version", sa.String(128), nullable=False),
        sa.Column("postprocessing_version", sa.String(128), nullable=False),
        sa.Column("confidence_threshold", sa.Float(), nullable=False),
        sa.Column("iou_threshold", sa.Float(), nullable=False),
        sa.Column("nms_mode", sa.String(64), nullable=False),
        sa.Column("nms_in_model", sa.Boolean(), nullable=False),
        sa.Column("class_map_version", sa.String(128), nullable=False),
        sa.Column("frame_sha256", sa.String(64), nullable=False),
        sa.Column("input_frame_sha256", sa.String(64), nullable=True),
        sa.Column("detections", sa.JSON(), nullable=False),
        sa.Column("stage_durations", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", name="uq_published_result_task"),
        sa.UniqueConstraint("attempt_id", name="uq_published_result_attempt"),
        sa.CheckConstraint(
            "confidence_threshold >= 0 AND confidence_threshold <= 1",
            name="ck_published_result_confidence_range",
        ),
        sa.CheckConstraint(
            "iou_threshold >= 0 AND iou_threshold <= 1",
            name="ck_published_result_iou_range",
        ),
    )
    op.create_index(
        "ix_published_inference_results_task_id", "published_inference_results", ["task_id"]
    )
    op.create_index(
        "ix_published_inference_results_organization_id",
        "published_inference_results",
        ["organization_id"],
    )
    op.create_index(
        "ix_published_inference_results_attempt_id", "published_inference_results", ["attempt_id"]
    )
    op.create_index(
        "ix_published_inference_results_artifact_id", "published_inference_results", ["artifact_id"]
    )

    op.create_table(
        "outbox_events",
        sa.Column("outbox_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(128), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("inference_tasks.task_id"), nullable=True),
        sa.Column("dispatch_seq", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("claim_owner", sa.String(255), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "dispatch_seq", "event_type", name="uq_outbox_task_dispatch_event"),
        sa.CheckConstraint(
            "dispatch_seq IS NULL OR dispatch_seq >= 1",
            name="ck_outbox_dispatch_positive",
        ),
        sa.CheckConstraint("publish_attempts >= 0", name="ck_outbox_attempts_nonnegative"),
    )
    op.create_index("ix_outbox_events_organization_id", "outbox_events", ["organization_id"])
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])
    op.create_index("ix_outbox_events_task_id", "outbox_events", ["task_id"])

    op.create_table(
        "message_quarantine",
        sa.Column("quarantine_id", sa.Uuid(), primary_key=True),
        sa.Column("stream_name", sa.String(255), nullable=False),
        sa.Column("message_id", sa.String(255), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("raw_payload", sa.LargeBinary(length=65536), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("inference_tasks.task_id"), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("stream_name", "message_id", name="uq_message_quarantine_stream_message"),
        sa.CheckConstraint(
            "length(raw_payload) <= 65536",
            name="ck_message_quarantine_payload_bounded",
        ),
    )
    op.create_index("ix_message_quarantine_task_id", "message_quarantine", ["task_id"])
    op.create_index(
        "ix_message_quarantine_organization_id", "message_quarantine", ["organization_id"]
    )

    op.create_table(
        "defect_episode",
        sa.Column("episode_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("defect_type", sa.String(255), nullable=False),
        sa.Column("spatial_zone", sa.String(255), nullable=False, server_default=sa.text("'GLOBAL'")),
        sa.Column("current_case_id", sa.Uuid(), sa.ForeignKey("defect_cases.case_id"), nullable=True),
        sa.Column("episode_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "organization_id",
            "camera_id",
            "defect_type",
            "spatial_zone",
            name="uq_defect_episode_tenant_camera_defect_zone",
        ),
        sa.CheckConstraint("length(spatial_zone) > 0", name="ck_defect_episode_zone_nonempty"),
    )
    op.create_index("ix_defect_episode_organization_id", "defect_episode", ["organization_id"])
    op.create_index("ix_defect_episode_camera_id", "defect_episode", ["camera_id"])
    op.create_index("ix_defect_episode_current_case_id", "defect_episode", ["current_case_id"])

    # Existing P0 events must remain valid while the nullable links are added.
    # Only then is the source-result uniqueness constraint installed.
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("inspection_events") as batch:
            batch.add_column(
                sa.Column(
                    "source_result_id",
                    sa.Uuid(),
                    sa.ForeignKey(
                        "published_inference_results.result_id",
                        name="fk_inspection_events_source_result",
                    ),
                    nullable=True,
                )
            )
            batch.add_column(
                sa.Column(
                    "evidence_artifact_id",
                    sa.Uuid(),
                    sa.ForeignKey(
                        "frame_artifacts.artifact_id",
                        name="fk_inspection_events_evidence_artifact",
                    ),
                    nullable=True,
                )
            )
            batch.create_unique_constraint(
                "uq_inspection_event_source_result", ["source_result_id"]
            )
    else:
        op.add_column(
            "inspection_events",
            sa.Column(
                "source_result_id",
                sa.Uuid(),
                sa.ForeignKey(
                    "published_inference_results.result_id",
                    name="fk_inspection_events_source_result",
                ),
                nullable=True,
            ),
        )
        op.add_column(
            "inspection_events",
            sa.Column(
                "evidence_artifact_id",
                sa.Uuid(),
                sa.ForeignKey(
                    "frame_artifacts.artifact_id",
                    name="fk_inspection_events_evidence_artifact",
                ),
                nullable=True,
            ),
        )
        op.create_unique_constraint(
            "uq_inspection_event_source_result", "inspection_events", ["source_result_id"]
        )
    op.create_index("ix_inspection_events_source_result_id", "inspection_events", ["source_result_id"])
    op.create_index(
        "ix_inspection_events_evidence_artifact_id",
        "inspection_events",
        ["evidence_artifact_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inspection_events_evidence_artifact_id", table_name="inspection_events")
    op.drop_index("ix_inspection_events_source_result_id", table_name="inspection_events")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("inspection_events") as batch:
            batch.drop_constraint("uq_inspection_event_source_result", type_="unique")
            batch.drop_column("evidence_artifact_id")
            batch.drop_column("source_result_id")
    else:
        op.drop_constraint("uq_inspection_event_source_result", "inspection_events", type_="unique")
        op.drop_column("inspection_events", "evidence_artifact_id")
        op.drop_column("inspection_events", "source_result_id")

    op.drop_index("ix_defect_episode_current_case_id", table_name="defect_episode")
    op.drop_index("ix_defect_episode_camera_id", table_name="defect_episode")
    op.drop_index("ix_defect_episode_organization_id", table_name="defect_episode")
    op.drop_table("defect_episode")
    op.drop_index("ix_message_quarantine_organization_id", table_name="message_quarantine")
    op.drop_index("ix_message_quarantine_task_id", table_name="message_quarantine")
    op.drop_table("message_quarantine")
    op.drop_index("ix_outbox_events_task_id", table_name="outbox_events")
    op.drop_index("ix_outbox_events_aggregate_id", table_name="outbox_events")
    op.drop_index("ix_outbox_events_organization_id", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index(
        "ix_published_inference_results_artifact_id", table_name="published_inference_results"
    )
    op.drop_index(
        "ix_published_inference_results_organization_id",
        table_name="published_inference_results",
    )
    op.drop_index(
        "ix_published_inference_results_attempt_id", table_name="published_inference_results"
    )
    op.drop_index(
        "ix_published_inference_results_task_id", table_name="published_inference_results"
    )
    op.drop_table("published_inference_results")
    op.drop_index("ix_inference_attempts_organization_id", table_name="inference_attempts")
    op.drop_index("ix_inference_attempts_task_id", table_name="inference_attempts")
    op.drop_table("inference_attempts")
    op.drop_index("ix_inference_tasks_camera_id", table_name="inference_tasks")
    op.drop_index("ix_inference_tasks_organization_id", table_name="inference_tasks")
    op.drop_table("inference_tasks")
    op.drop_index("ix_frame_artifacts_stream_session_id", table_name="frame_artifacts")
    op.drop_index("ix_frame_artifacts_camera_id", table_name="frame_artifacts")
    op.drop_index("ix_frame_artifacts_organization_id", table_name="frame_artifacts")
    op.drop_table("frame_artifacts")
    op.drop_index("ix_inspection_sessions_line_id", table_name="inspection_sessions")
    op.drop_index("ix_inspection_sessions_camera_id", table_name="inspection_sessions")
    op.drop_index("ix_inspection_sessions_organization_id", table_name="inspection_sessions")
    op.drop_table("inspection_sessions")
    op.drop_table("camera_inference_state")
