"""PostgreSQL/SQLAlchemy ownership transitions for ingestion sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.task_models import InspectionSessionRow
from odp_api.ports.inspection_sessions import InspectionSession, InspectionSessionPort


class SqlAlchemyInspectionSessionRepository(InspectionSessionPort):
    """Guard status transitions with row locks and conditional updates."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[Session], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def claim_start_requests(self, process_id: str, limit: int) -> list[InspectionSession]:
        _validate_process_and_limit(process_id, limit)
        with self._session_factory() as session:
            try:
                now = self._db_now(session)
                rows = session.scalars(
                    select(InspectionSessionRow)
                    .where(InspectionSessionRow.status == "START_REQUESTED")
                    .order_by(InspectionSessionRow.created_at, InspectionSessionRow.session_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
                for row in rows:
                    row.status = "RUNNING"
                    row.ingestor_process_id = process_id
                    row.started_at = now
                    row.heartbeat_at = now
                    row.error_code = None
                    row.error_detail = None
                    row.updated_at = now
                result = [_session_from_row(row) for row in rows]
                session.commit()
                return result
            except BaseException:
                session.rollback()
                raise

    def heartbeat(self, session_id, process_id: str, now: datetime) -> bool:
        _validate_process(process_id)
        with self._session_factory() as session:
            current = self._db_now(session)
            result = session.execute(
                update(InspectionSessionRow)
                .where(
                    InspectionSessionRow.session_id == session_id,
                    InspectionSessionRow.status == "RUNNING",
                    InspectionSessionRow.ingestor_process_id == process_id,
                )
                .values(heartbeat_at=current, updated_at=current)
            )
            session.commit()
            return result.rowcount == 1

    def claim_stop_requests(self, process_id: str, limit: int) -> list[InspectionSession]:
        _validate_process_and_limit(process_id, limit)
        with self._session_factory() as session:
            try:
                now = self._db_now(session)
                rows = session.scalars(
                    select(InspectionSessionRow)
                    .where(
                        InspectionSessionRow.status == "STOP_REQUESTED",
                        (InspectionSessionRow.ingestor_process_id.is_(None))
                        | (InspectionSessionRow.ingestor_process_id == process_id),
                    )
                    .order_by(InspectionSessionRow.updated_at, InspectionSessionRow.session_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
                for row in rows:
                    row.status = "STOPPED"
                    row.ingestor_process_id = process_id
                    row.stopped_at = now
                    row.updated_at = now
                result = [_session_from_row(row) for row in rows]
                session.commit()
                return result
            except BaseException:
                session.rollback()
                raise

    def mark_failed(
        self,
        session_id,
        process_id: str,
        error_code: str,
        detail: str,
        now: datetime,
    ) -> bool:
        _validate_process(process_id)
        if not error_code.strip() or not detail.strip():
            raise ValueError("session failure code and detail are required")
        with self._session_factory() as session:
            current = self._db_now(session)
            result = session.execute(
                update(InspectionSessionRow)
                .where(
                    InspectionSessionRow.session_id == session_id,
                    InspectionSessionRow.status.in_(("START_REQUESTED", "RUNNING")),
                    InspectionSessionRow.ingestor_process_id == process_id,
                )
                .values(
                    status="FAILED",
                    error_code=error_code[:128],
                    error_detail=detail[:4096],
                    updated_at=current,
                )
            )
            session.commit()
            return result.rowcount == 1

    def _db_now(self, session: Session) -> datetime:
        value = self._clock(session) if self._clock is not None else session.scalar(select(func.now()))
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _validate_process(process_id: str) -> None:
    if not process_id.strip() or len(process_id) > 255:
        raise ValueError("process_id must be a bounded non-empty string")


def _validate_process_and_limit(process_id: str, limit: int) -> None:
    _validate_process(process_id)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be positive")


def _session_from_row(row: InspectionSessionRow) -> InspectionSession:
    return InspectionSession(
        session_id=row.session_id,
        organization_id=row.organization_id,
        camera_id=row.camera_id,
        line_id=row.line_id,
        source_type=row.source_type,
        sanitized_uri=row.sanitized_uri,
        secret_reference=row.secret_reference,
        status=row.status,
    )


__all__ = ["SqlAlchemyInspectionSessionRepository"]
