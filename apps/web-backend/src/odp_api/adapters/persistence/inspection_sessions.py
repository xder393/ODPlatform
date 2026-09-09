"""PostgreSQL/SQLAlchemy ownership transitions for ingestion sessions."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.ingestion_ownership import (
    database_now,
    owns_live_session,
)
from odp_api.adapters.persistence.task_models import InspectionSessionRow
from odp_api.ports.inspection_sessions import (
    ClaimedInspectionSession,
    IngestionClaim,
    InspectionSession,
    InspectionSessionPort,
)


class SqlAlchemyInspectionSessionRepository(InspectionSessionPort):
    """Guard status transitions with row locks and conditional updates."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_duration: timedelta | float = timedelta(seconds=30),
        clock: Callable[[Session], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._lease_duration = _validate_lease_duration(lease_duration)

    def claim_available(
        self,
        process_id: str,
        instance_id: UUID,
        limit: int,
    ) -> list[ClaimedInspectionSession]:
        """Claim start requests and expired RUNNING sessions for one instance."""

        _validate_process_and_limit(process_id, limit)
        with self._session_factory() as session:
            try:
                claimed: list[ClaimedInspectionSession] = []
                processed_ids: set[UUID] = set()
                last_updated_at = None
                last_session_id = None
                while len(claimed) < limit:
                    predicates = [
                        InspectionSessionRow.status.in_(("START_REQUESTED", "RUNNING"))
                    ]
                    if processed_ids:
                        predicates.append(
                            InspectionSessionRow.session_id.not_in(processed_ids)
                        )
                    if last_updated_at is not None:
                        predicates.append(
                            or_(
                                InspectionSessionRow.updated_at > last_updated_at,
                                and_(
                                    InspectionSessionRow.updated_at == last_updated_at,
                                    InspectionSessionRow.session_id > last_session_id,
                                ),
                            )
                        )
                    rows = session.scalars(
                        select(InspectionSessionRow)
                        .where(*predicates)
                        .order_by(
                            InspectionSessionRow.updated_at,
                            InspectionSessionRow.session_id,
                        )
                        .limit(limit - len(claimed))
                        .with_for_update(skip_locked=True)
                    ).all()
                    if not rows:
                        break
                    for row in rows:
                        processed_ids.add(row.session_id)
                        last_updated_at = row.updated_at
                        last_session_id = row.session_id
                        now = self._db_now(session)
                        was_start_request = row.status == "START_REQUESTED"
                        if was_start_request:
                            eligible = True
                        else:
                            eligible = row.lease_expires_at is None or _as_utc(
                                row.lease_expires_at
                            ) <= now
                        if not eligible:
                            continue

                        next_generation = int(row.ingestion_generation or 0) + 1
                        row.status = "RUNNING"
                        row.ingestor_process_id = process_id
                        row.owner_instance_id = instance_id
                        row.ingestion_generation = next_generation
                        row.lease_expires_at = now + self._lease_duration
                        row.heartbeat_at = now
                        row.error_code = None
                        row.error_detail = None
                        if row.started_at is None or was_start_request:
                            row.started_at = now
                        row.updated_at = now
                        claimed.append(
                            ClaimedInspectionSession(
                                session=_session_from_row(row),
                                claim=IngestionClaim(
                                    organization_id=row.organization_id,
                                    camera_id=row.camera_id,
                                    session_id=row.session_id,
                                    owner_instance_id=instance_id,
                                    generation=next_generation,
                                ),
                                initial_sequence=int(row.last_reserved_sequence or 0),
                            )
                        )
                        if len(claimed) >= limit:
                            break
                session.commit()
                return claimed
            except BaseException:
                session.rollback()
                raise

    def renew(self, claim: IngestionClaim) -> bool:
        """Renew a still-live claim after locking and rechecking database time."""

        _validate_claim(claim)
        with self._session_factory() as session:
            try:
                row = _locked_row(session, claim.session_id)
                now = self._db_now(session)
                if row is None or not owns_live_session(row, claim, now):
                    session.commit()
                    return False
                row.lease_expires_at = now + self._lease_duration
                row.heartbeat_at = now
                row.updated_at = now
                session.commit()
                return True
            except BaseException:
                session.rollback()
                raise

    def stop_candidates(self, instance_id: UUID, limit: int) -> list[IngestionClaim]:
        """Return STOP_REQUESTED claims owned by this instance, including expired ones."""

        _validate_limit(limit)
        with self._session_factory() as session:
            try:
                rows = session.scalars(
                    select(InspectionSessionRow)
                    .where(
                        InspectionSessionRow.status == "STOP_REQUESTED",
                        InspectionSessionRow.owner_instance_id == instance_id,
                    )
                    .order_by(
                        InspectionSessionRow.updated_at,
                        InspectionSessionRow.session_id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
                claims = [_claim_from_row(row) for row in rows]
                session.commit()
                return claims
            except BaseException:
                session.rollback()
                raise

    def finish_stop(self, claim: IngestionClaim) -> bool:
        """Finalize a matching STOP_REQUESTED row, even after its lease expires."""

        _validate_claim(claim)
        with self._session_factory() as session:
            try:
                row = _locked_row(session, claim.session_id)
                now = self._db_now(session)
                if row is None or row.status != "STOP_REQUESTED" or not _matches_claim(
                    row, claim
                ):
                    session.commit()
                    return False
                row.status = "STOPPED"
                row.owner_instance_id = None
                row.lease_expires_at = None
                row.heartbeat_at = None
                row.stopped_at = now
                row.updated_at = now
                session.commit()
                return True
            except BaseException:
                session.rollback()
                raise

    def finalize_expired_stops(self, limit: int) -> int:
        """Stop orphaned STOP_REQUESTED rows without reopening their sources."""

        _validate_limit(limit)
        with self._session_factory() as session:
            try:
                finalized = 0
                last_updated_at = None
                last_session_id = None
                while finalized < limit:
                    predicates = [InspectionSessionRow.status == "STOP_REQUESTED"]
                    if last_updated_at is not None:
                        predicates.append(
                            or_(
                                InspectionSessionRow.updated_at > last_updated_at,
                                and_(
                                    InspectionSessionRow.updated_at == last_updated_at,
                                    InspectionSessionRow.session_id > last_session_id,
                                ),
                            )
                        )
                    rows = session.scalars(
                        select(InspectionSessionRow)
                        .where(*predicates)
                        .order_by(
                            InspectionSessionRow.updated_at,
                            InspectionSessionRow.session_id,
                        )
                        .limit(limit - finalized)
                        .with_for_update(skip_locked=True)
                    ).all()
                    if not rows:
                        break
                    for row in rows:
                        last_updated_at = row.updated_at
                        last_session_id = row.session_id
                        now = self._db_now(session)
                        if (
                            row.owner_instance_id is not None
                            and row.lease_expires_at is not None
                            and _as_utc(row.lease_expires_at) > now
                        ):
                            continue
                        row.status = "STOPPED"
                        row.owner_instance_id = None
                        row.lease_expires_at = None
                        row.heartbeat_at = None
                        row.stopped_at = now
                        row.updated_at = now
                        finalized += 1
                        if finalized >= limit:
                            break
                session.commit()
                return finalized
            except BaseException:
                session.rollback()
                raise

    def release(self, claim: IngestionClaim) -> bool:
        """Expire only a matching live RUNNING lease, preserving RUNNING status."""

        _validate_claim(claim)
        with self._session_factory() as session:
            try:
                row = _locked_row(session, claim.session_id)
                now = self._db_now(session)
                if row is None or not owns_live_session(row, claim, now):
                    session.commit()
                    return False
                row.lease_expires_at = now
                row.heartbeat_at = now
                row.updated_at = now
                session.commit()
                return True
            except BaseException:
                session.rollback()
                raise

    def fail_claim(self, claim: IngestionClaim, error_code: str, detail: str) -> bool:
        """Publish failure only while the supplied claim is the live owner."""

        _validate_claim(claim)
        if not error_code.strip() or not detail.strip():
            raise ValueError("session failure code and detail are required")
        with self._session_factory() as session:
            try:
                row = _locked_row(session, claim.session_id)
                now = self._db_now(session)
                if row is None or not owns_live_session(row, claim, now):
                    session.commit()
                    return False
                row.status = "FAILED"
                row.owner_instance_id = None
                row.lease_expires_at = None
                row.heartbeat_at = None
                row.error_code = error_code[:128]
                row.error_detail = detail[:4096]
                row.updated_at = now
                session.commit()
                return True
            except BaseException:
                session.rollback()
                raise

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
        return database_now(session, self._clock)


def _validate_lease_duration(value: timedelta | float) -> timedelta:
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        raise TypeError("lease duration must be a positive finite duration")
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("lease duration must be a positive finite duration")
    return timedelta(seconds=seconds)


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be positive")


def _validate_claim(claim: IngestionClaim) -> None:
    if not isinstance(claim, IngestionClaim):
        raise TypeError("claim must be an IngestionClaim")
    if claim.generation < 1:
        raise ValueError("claim generation must be positive")


def _locked_row(session: Session, session_id: UUID) -> InspectionSessionRow | None:
    return session.scalar(
        select(InspectionSessionRow)
        .where(InspectionSessionRow.session_id == session_id)
        .with_for_update()
    )


def _matches_claim(row: InspectionSessionRow, claim: IngestionClaim) -> bool:
    return (
        row.organization_id == claim.organization_id
        and row.camera_id == claim.camera_id
        and row.session_id == claim.session_id
        and row.owner_instance_id == claim.owner_instance_id
        and row.ingestion_generation == claim.generation
    )


def _claim_from_row(row: InspectionSessionRow) -> IngestionClaim:
    if row.owner_instance_id is None:
        raise RuntimeError("session has no ingestion owner")
    return IngestionClaim(
        organization_id=row.organization_id,
        camera_id=row.camera_id,
        session_id=row.session_id,
        owner_instance_id=row.owner_instance_id,
        generation=int(row.ingestion_generation or 0),
    )


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        raise RuntimeError("session lease has no expiry")
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
