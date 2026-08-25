"""Database rows for durable P0 business state.

UUID and JSON types intentionally use SQLAlchemy's portable implementations so
the same metadata works against SQLite in tests and PostgreSQL in Compose.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid


class Base(DeclarativeBase):
    pass


class ActorRow(Base):
    __tablename__ = "actors"

    actor_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    role: Mapped[str] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())


class ActorLineGrantRow(Base):
    __tablename__ = "actor_line_grants"

    actor_id: Mapped[UUID] = mapped_column(ForeignKey("actors.actor_id"), primary_key=True)
    line_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)


class PasswordCredentialRow(Base):
    __tablename__ = "password_credentials"

    actor_id: Mapped[UUID] = mapped_column(ForeignKey("actors.actor_id"), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))


class DefectCaseRow(Base):
    __tablename__ = "defect_cases"

    case_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    status: Mapped[str] = mapped_column(String(32))
    assignee_id: Mapped[UUID | None] = mapped_column(Uuid)
    last_transition_actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    line_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    product_category: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InspectionEventRow(Base):
    __tablename__ = "inspection_events"
    __table_args__ = (
        UniqueConstraint("source_result_id", name="uq_inspection_event_source_result"),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("defect_cases.case_id"), index=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    camera_id: Mapped[UUID] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    defect_class: Mapped[str] = mapped_column(String(255))
    confidence: Mapped[float] = mapped_column(Float)
    model_release: Mapped[str] = mapped_column(String(255))
    preprocessing_parameters: Mapped[list[list[str]]] = mapped_column(JSON)
    threshold: Mapped[float] = mapped_column(Float)
    input_frame_sha256: Mapped[str] = mapped_column(String(64))
    line_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    source_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "published_inference_results.result_id",
            name="fk_inspection_events_source_result",
        ),
        index=True,
    )
    evidence_artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("frame_artifacts.artifact_id", name="fk_inspection_events_evidence_artifact"),
        index=True,
    )


class CaseTransitionRow(Base):
    __tablename__ = "case_transitions"

    transition_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("defect_cases.case_id"), index=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID | None] = mapped_column(Uuid)


class AuditChainHeadRow(Base):
    __tablename__ = "audit_chain_heads"

    organization_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    head_hash: Mapped[str] = mapped_column(String(64))


class AuditLogRow(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (UniqueConstraint("organization_id", "sequence"),)

    audit_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    resource_type: Mapped[str] = mapped_column(String(128))
    resource_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(128))
    change_summary: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID | None] = mapped_column(Uuid)
    request_ip: Mapped[str | None] = mapped_column(String(64))
    previous_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))


class AlertRow(Base):
    __tablename__ = "alerts"

    alert_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("inspection_events.event_id"))
    line_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    alert_type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InspectionAlertFeedRow(Base):
    """Durable event facts with a database-assigned, monotonic feed cursor."""

    __tablename__ = "inspection_alerts"

    cursor: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    __table_args__ = (UniqueConstraint("organization_id", "event_id", name="uq_inspection_alerts_org_event"),)

    event_id: Mapped[UUID] = mapped_column(Uuid)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    line_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebSocketTicketRow(Base):
    __tablename__ = "websocket_tickets"

    ticket_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("actors.actor_id"))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReauthenticationMarkerRow(Base):
    __tablename__ = "reauthentication_markers"

    actor_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
