"""Tenant-scoped read models for inference task operations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
)


class TaskAttemptDiagnostic(BaseModel):
    attempt_id: UUID
    attempt_no: int
    fence_token: int
    worker_id: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    error_code: str | None
    duration_ms: float | None


class TaskDispatchDiagnostic(BaseModel):
    outbox_id: UUID
    event_type: str
    schema_version: int
    dispatch_seq: int | None
    available_at: datetime
    published_at: datetime | None
    publish_attempts: int
    last_error: str | None


class TaskDiagnostic(BaseModel):
    task_id: UUID
    organization_id: UUID
    camera_id: UUID
    line_id: UUID | None
    artifact_id: UUID
    status: str
    dispatch_seq: int
    attempt_count: int
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime
    attempts: list[TaskAttemptDiagnostic]
    dispatches: list[TaskDispatchDiagnostic]


class TaskDiagnosticListResponse(BaseModel):
    items: list[TaskDiagnostic]


class TaskReplayResponse(BaseModel):
    task_id: UUID
    source_task_id: UUID
    status: str
    dispatch_seq: int


class TaskDiagnosticsService:
    """Read-only diagnostics over the PostgreSQL task control plane."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def list(
        self,
        organization_id: UUID,
        line_ids: frozenset[UUID] | None,
        *,
        limit: int,
        status: str | None = None,
        camera_id: UUID | None = None,
    ) -> list[TaskDiagnostic]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._sessions() as session:
            rows = self._task_rows(
                session,
                organization_id,
                line_ids,
                status=status,
                camera_id=camera_id,
                limit=limit,
            )
            return [self._diagnostic(session, row, line_ids) for row in rows]

    def get(
        self,
        organization_id: UUID,
        task_id: UUID,
        line_ids: frozenset[UUID] | None,
    ) -> TaskDiagnostic | None:
        with self._sessions() as session:
            row = session.scalar(
                select(InferenceTaskRow).where(
                    InferenceTaskRow.task_id == task_id,
                    InferenceTaskRow.organization_id == organization_id,
                )
            )
            if row is None:
                return None
            return self._diagnostic(session, row, line_ids)

    def _task_rows(
        self,
        session: Session,
        organization_id: UUID,
        line_ids: frozenset[UUID] | None,
        *,
        status: str | None,
        camera_id: UUID | None,
        limit: int,
    ) -> list[InferenceTaskRow]:
        statement = (
            select(InferenceTaskRow)
            .join(FrameArtifactRow, FrameArtifactRow.artifact_id == InferenceTaskRow.artifact_id)
            .join(
                InspectionSessionRow,
                InspectionSessionRow.session_id == FrameArtifactRow.stream_session_id,
            )
            .where(InferenceTaskRow.organization_id == organization_id)
            .order_by(InferenceTaskRow.created_at.desc(), InferenceTaskRow.task_id)
            .limit(limit)
        )
        if line_ids is not None:
            if not line_ids:
                return []
            statement = statement.where(InspectionSessionRow.line_id.in_(line_ids))
        if status is not None:
            statement = statement.where(InferenceTaskRow.status == status)
        if camera_id is not None:
            statement = statement.where(InferenceTaskRow.camera_id == camera_id)
        return list(session.scalars(statement))

    def _diagnostic(
        self,
        session: Session,
        row: InferenceTaskRow,
        line_ids: frozenset[UUID] | None,
    ) -> TaskDiagnostic:
        line_id = session.scalar(
            select(InspectionSessionRow.line_id)
            .join(FrameArtifactRow, FrameArtifactRow.stream_session_id == InspectionSessionRow.session_id)
            .where(
                FrameArtifactRow.artifact_id == row.artifact_id,
                InspectionSessionRow.organization_id == row.organization_id,
            )
        )
        if line_ids is not None and line_id not in line_ids:
            # The router converts this into a tenant-safe 404 instead of
            # revealing whether another line owns the task.
            raise LookupError("task is outside actor line scope")
        attempts = session.scalars(
            select(InferenceAttemptRow)
            .where(
                InferenceAttemptRow.task_id == row.task_id,
                InferenceAttemptRow.organization_id == row.organization_id,
            )
            .order_by(InferenceAttemptRow.attempt_no)
        ).all()
        dispatches = session.scalars(
            select(OutboxEventRow)
            .where(
                OutboxEventRow.task_id == row.task_id,
                OutboxEventRow.organization_id == row.organization_id,
            )
            .order_by(OutboxEventRow.dispatch_seq, OutboxEventRow.created_at)
        ).all()
        return TaskDiagnostic(
            task_id=row.task_id,
            organization_id=row.organization_id,
            camera_id=row.camera_id,
            line_id=line_id,
            artifact_id=row.artifact_id,
            status=row.status,
            dispatch_seq=row.dispatch_seq,
            attempt_count=row.attempt_count,
            error_code=row.error_code,
            error_detail=row.error_detail,
            created_at=_utc(row.created_at),
            updated_at=_utc(row.updated_at),
            attempts=[
                TaskAttemptDiagnostic(
                    attempt_id=attempt.attempt_id,
                    attempt_no=attempt.attempt_no,
                    fence_token=attempt.fence_token,
                    worker_id=attempt.worker_id,
                    started_at=_utc(attempt.started_at),
                    finished_at=_optional_utc(attempt.finished_at),
                    outcome=attempt.outcome,
                    error_code=attempt.error_code,
                    duration_ms=attempt.duration_ms,
                )
                for attempt in attempts
            ],
            dispatches=[
                TaskDispatchDiagnostic(
                    outbox_id=event.outbox_id,
                    event_type=event.event_type,
                    schema_version=event.schema_version,
                    dispatch_seq=event.dispatch_seq,
                    available_at=_utc(event.available_at),
                    published_at=_optional_utc(event.published_at),
                    publish_attempts=event.publish_attempts,
                    last_error=event.last_error,
                )
                for event in dispatches
            ],
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _utc(value) if value is not None else None


__all__ = [
    "TaskAttemptDiagnostic",
    "TaskDiagnostic",
    "TaskDiagnosticListResponse",
    "TaskDiagnosticsService",
    "TaskDispatchDiagnostic",
    "TaskReplayResponse",
]
