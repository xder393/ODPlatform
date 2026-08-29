"""SQLAlchemy rows for the PostgreSQL-owned realtime inference control plane.

These rows intentionally share the P0 :class:`Base`: Alembic and the runtime
therefore see one metadata graph, while the P0 SQLite task adapter remains
independent until the P1B runtime cutover.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from odp_api.adapters.persistence.models import Base


class CameraInferenceStateRow(Base):
    """Per-tenant/per-camera serialization anchor for admission and fencing."""

    __tablename__ = "camera_inference_state"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "camera_id",
            name="uq_camera_inference_state_tenant_camera",
        ),
        CheckConstraint("ready_count >= 0", name="ck_camera_state_ready_nonnegative"),
        CheckConstraint("ready_count <= 2", name="ck_camera_state_ready_maximum"),
        CheckConstraint("version >= 0", name="ck_camera_state_version_nonnegative"),
    )

    organization_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    camera_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    running_task_id: Mapped[UUID | None] = mapped_column(Uuid)
    ready_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    reservation_id: Mapped[UUID | None] = mapped_column(Uuid)
    reservation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InspectionSessionRow(Base):
    """A tenant-scoped camera stream session and its sanitized source URI."""

    __tablename__ = "inspection_sessions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_inspection_session_tenant_key",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    camera_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    line_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sanitized_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret_reference: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FrameArtifactRow(Base):
    """Metadata and lifecycle state for one immutable uploaded frame."""

    __tablename__ = "frame_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "camera_id",
            "stream_session_id",
            "frame_sequence",
            name="uq_frame_artifact_tenant_camera_session_sequence",
        ),
        CheckConstraint(
            "frame_sequence >= 1", name="ck_frame_artifact_sequence_positive"
        ),
        CheckConstraint(
            "content_length IS NULL OR content_length >= 0",
            name="ck_frame_artifact_content_length_nonnegative",
        ),
    )

    artifact_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    camera_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    stream_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("inspection_sessions.session_id"), nullable=False, index=True
    )
    frame_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    object_key: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_length: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InferenceTaskRow(Base):
    """Durable task state, dispatch sequence, and current worker lease."""

    __tablename__ = "inference_tasks"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_inference_task_tenant_key"
        ),
        CheckConstraint(
            "dispatch_seq >= 1", name="ck_inference_task_dispatch_positive"
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_inference_task_attempt_nonnegative"
        ),
        CheckConstraint("fence_token >= 0", name="ck_inference_task_fence_nonnegative"),
    )

    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    camera_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    artifact_id: Mapped[UUID] = mapped_column(
        ForeignKey("frame_artifacts.artifact_id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    fence_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InferenceAttemptRow(Base):
    """Append-only execution ownership and outcome record."""

    __tablename__ = "inference_attempts"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "attempt_no", name="uq_inference_attempt_task_number"
        ),
        UniqueConstraint(
            "task_id", "fence_token", name="uq_inference_attempt_task_fence"
        ),
        CheckConstraint("attempt_no >= 1", name="ck_inference_attempt_number_positive"),
        CheckConstraint("fence_token >= 1", name="ck_inference_attempt_fence_positive"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_inference_attempt_duration_nonnegative",
        ),
    )

    attempt_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("inference_tasks.task_id"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_detail: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PublishedInferenceResultRow(Base):
    """A fenced, immutable inference result used by business effects."""

    __tablename__ = "published_inference_results"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_published_result_task"),
        UniqueConstraint("attempt_id", name="uq_published_result_attempt"),
        CheckConstraint(
            "confidence_threshold >= 0 AND confidence_threshold <= 1",
            name="ck_published_result_confidence_range",
        ),
        CheckConstraint(
            "iou_threshold >= 0 AND iou_threshold <= 1",
            name="ck_published_result_iou_range",
        ),
    )

    result_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("inference_tasks.task_id"), nullable=False, index=True
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("inference_attempts.attempt_id"), nullable=False, index=True
    )
    artifact_id: Mapped[UUID] = mapped_column(
        ForeignKey("frame_artifacts.artifact_id"), nullable=False, index=True
    )
    model_release: Mapped[str] = mapped_column(String(255), nullable=False)
    model_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    onnxruntime_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_provider: Mapped[str] = mapped_column(String(128), nullable=False)
    actual_input_shape: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    preprocessing_version: Mapped[str] = mapped_column(String(128), nullable=False)
    postprocessing_version: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    iou_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    nms_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    nms_in_model: Mapped[bool] = mapped_column(Boolean, nullable=False)
    class_map_version: Mapped[str] = mapped_column(String(128), nullable=False)
    frame_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # ``input_frame_sha256`` keeps the P0 naming available to consumers while
    # ``frame_sha256`` is the P1 command/storage spelling.
    input_frame_sha256: Mapped[str | None] = mapped_column(String(64))
    detections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    stage_durations: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    correlation_id: Mapped[UUID | None] = mapped_column(Uuid)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEventRow(Base):
    """Transactional outbox message awaiting delivery or retained after publish."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "dispatch_seq",
            "event_type",
            name="uq_outbox_task_dispatch_event",
        ),
        CheckConstraint(
            "dispatch_seq IS NULL OR dispatch_seq >= 1",
            name="ck_outbox_dispatch_positive",
        ),
        CheckConstraint("publish_attempts >= 0", name="ck_outbox_attempts_nonnegative"),
    )

    outbox_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("inference_tasks.task_id"), index=True
    )
    dispatch_seq: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claim_owner: Mapped[str | None] = mapped_column(String(255))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MessageQuarantineRow(Base):
    """Bounded record of an event that cannot safely enter the task stream."""

    __tablename__ = "message_quarantine"
    __table_args__ = (
        UniqueConstraint(
            "stream_name", "message_id", name="uq_message_quarantine_stream_message"
        ),
        CheckConstraint(
            "length(raw_payload) <= 65536",
            name="ck_message_quarantine_payload_bounded",
        ),
    )

    quarantine_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    stream_name: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_payload: Mapped[bytes] = mapped_column(
        LargeBinary(length=65536), nullable=False
    )
    error: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("inference_tasks.task_id"), index=True
    )
    organization_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    quarantined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DefectEpisodeRow(Base):
    """Stable deduplication key for an active defect episode."""

    __tablename__ = "defect_episode"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "camera_id",
            "defect_type",
            "spatial_zone",
            name="uq_defect_episode_tenant_camera_defect_zone",
        ),
        CheckConstraint(
            "length(spatial_zone) > 0", name="ck_defect_episode_zone_nonempty"
        ),
    )

    episode_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    camera_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    defect_type: Mapped[str] = mapped_column(String(255), nullable=False)
    spatial_zone: Mapped[str] = mapped_column(
        String(255), nullable=False, default="GLOBAL", server_default=text("'GLOBAL'")
    )
    current_case_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("defect_cases.case_id"), index=True
    )
    episode_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
