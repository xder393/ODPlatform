"""Transactional inspection-session lifecycle service."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.task_models import InspectionSessionRow
from odp_api.modules.inspection_sessions.models import (
    InspectionSessionCreate,
    InspectionSessionResponse,
)
from odp_api.processes.frame_ingestor import sanitize_source_uri


class SessionIdempotencyConflict(ValueError):
    """The same idempotency key was reused with different source settings."""


class InspectionSessionService:
    """Own session state transitions; authorization stays in the HTTP layer."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(
        self,
        organization_id: UUID,
        request: InspectionSessionCreate,
        idempotency_key: str,
    ) -> InspectionSessionResponse:
        key = idempotency_key.strip()
        if not key or len(key) > 255:
            raise ValueError("Idempotency-Key is required and must be bounded")
        sanitized = sanitize_source_uri(request.source_ref)
        now = datetime.now(UTC)
        with self._sessions() as session:
            existing = session.scalar(
                select(InspectionSessionRow).where(
                    InspectionSessionRow.organization_id == organization_id,
                    InspectionSessionRow.idempotency_key == key,
                )
            )
            if existing is not None:
                if not _same_request(existing, request, sanitized):
                    raise SessionIdempotencyConflict("Idempotency-Key payload differs")
                return _response(existing)
            row = InspectionSessionRow(
                session_id=uuid4(),
                organization_id=organization_id,
                camera_id=request.camera_id,
                line_id=request.line_id,
                source_type=request.source_type,
                sanitized_uri=sanitized,
                secret_reference=request.secret_ref,
                status="START_REQUESTED",
                ingestor_process_id=None,
                idempotency_key=key,
                heartbeat_at=None,
                error_code=None,
                error_detail=None,
                started_at=now,
                stopped_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                winner = session.scalar(
                    select(InspectionSessionRow).where(
                        InspectionSessionRow.organization_id == organization_id,
                        InspectionSessionRow.idempotency_key == key,
                    )
                )
                if winner is None or not _same_request(winner, request, sanitized):
                    raise SessionIdempotencyConflict("Idempotency-Key payload differs") from None
                return _response(winner)
            return _response(row)

    def get(self, organization_id: UUID, session_id: UUID) -> InspectionSessionResponse | None:
        with self._sessions() as session:
            row = session.scalar(
                select(InspectionSessionRow).where(
                    InspectionSessionRow.organization_id == organization_id,
                    InspectionSessionRow.session_id == session_id,
                )
            )
            return _response(row) if row is not None else None

    def list(
        self,
        organization_id: UUID,
        authorized_line_ids: frozenset[UUID] | None,
        limit: int,
    ) -> list[InspectionSessionResponse]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._sessions() as session:
            statement = (
                select(InspectionSessionRow)
                .where(InspectionSessionRow.organization_id == organization_id)
                .order_by(InspectionSessionRow.created_at.desc())
                .limit(limit)
            )
            if authorized_line_ids is not None:
                if not authorized_line_ids:
                    return []
                statement = statement.where(InspectionSessionRow.line_id.in_(authorized_line_ids))
            return [_response(row) for row in session.scalars(statement)]

    def request_stop(self, organization_id: UUID, session_id: UUID) -> InspectionSessionResponse | None:
        with self._sessions() as session:
            row = session.scalar(
                select(InspectionSessionRow)
                .where(
                    InspectionSessionRow.organization_id == organization_id,
                    InspectionSessionRow.session_id == session_id,
                )
                .with_for_update()
            )
            if row is None:
                return None
            if row.status in {"START_REQUESTED", "RUNNING"}:
                row.status = "STOP_REQUESTED"
                row.updated_at = datetime.now(UTC)
                session.commit()
            return _response(row)


def _same_request(
    row: InspectionSessionRow,
    request: InspectionSessionCreate,
    sanitized: str,
) -> bool:
    return (
        row.camera_id == request.camera_id
        and row.line_id == request.line_id
        and row.source_type == request.source_type
        and row.sanitized_uri == sanitized
        and row.secret_reference == request.secret_ref
    )


def _response(row: InspectionSessionRow) -> InspectionSessionResponse:
    return InspectionSessionResponse(
        session_id=row.session_id,
        organization_id=row.organization_id,
        camera_id=row.camera_id,
        line_id=row.line_id,
        source_type=row.source_type,
        sanitized_uri=row.sanitized_uri,
        secret_reference=row.secret_reference,
        status=row.status,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = ["InspectionSessionService", "SessionIdempotencyConflict"]
