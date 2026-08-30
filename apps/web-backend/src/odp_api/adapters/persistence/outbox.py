"""SQLAlchemy transactional-Outbox claim and completion adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.task_models import InferenceTaskRow, OutboxEventRow
from odp_api.ports.events import ClaimedOutboxEvent, OutboxRepositoryPort


class SqlAlchemyOutboxRepository(OutboxRepositoryPort):
    """System-scoped repository for claiming and updating Outbox rows.

    Selection is intentionally cross-tenant because a relay process is a
    system capability.  Every mutation still matches the row's tenant ID and
    claim owner, so a stale worker cannot update another tenant's event or a
    reclaimed claim.  Pass ``clock`` in tests; production defaults to the
    database clock for lease and availability decisions.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[Session], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def claim_ready(
        self,
        limit: int,
        now: datetime,
        claim_owner: str,
        claim_lease: timedelta,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        if limit < 1:
            return ()
        if not claim_owner.strip():
            raise ValueError("claim_owner is required")
        if claim_lease <= timedelta(0):
            raise ValueError("claim_lease must be positive")

        with self._session_factory() as session:
            try:
                current = self._database_now(session, now)
                rows = session.execute(
                    select(OutboxEventRow, InferenceTaskRow)
                    .outerjoin(
                        InferenceTaskRow,
                        and_(
                            InferenceTaskRow.task_id == OutboxEventRow.task_id,
                            InferenceTaskRow.organization_id
                            == OutboxEventRow.organization_id,
                        ),
                    )
                    .where(
                        OutboxEventRow.published_at.is_(None),
                        OutboxEventRow.available_at <= current,
                        or_(
                            OutboxEventRow.claim_expires_at.is_(None),
                            OutboxEventRow.claim_expires_at <= current,
                        ),
                    )
                    .order_by(
                        OutboxEventRow.available_at,
                        OutboxEventRow.created_at,
                        OutboxEventRow.outbox_id,
                    )
                    .with_for_update(of=OutboxEventRow, skip_locked=True)
                    .limit(limit)
                ).all()

                claimed: list[ClaimedOutboxEvent] = []
                for row, task in rows:
                    # Loading the tenant-matched task in the same query keeps
                    # the authority boundary explicit.  A missing task does
                    # not delete the Outbox row; the relay will record a
                    # retryable construction failure instead.
                    row.claim_owner = claim_owner
                    row.claim_expires_at = current + claim_lease
                    row.updated_at = current
                    claimed.append(
                        _detached_event(row, current, task_found=task is not None)
                    )
                session.commit()
                return tuple(claimed)
            except BaseException:
                session.rollback()
                raise

    def mark_published(
        self,
        outbox_id: UUID,
        organization_id: UUID,
        claim_owner: str,
        published_at: datetime,
    ) -> bool:
        with self._session_factory() as session:
            try:
                current = self._database_now(session, published_at)
                result = session.execute(
                    update(OutboxEventRow)
                    .where(
                        OutboxEventRow.outbox_id == outbox_id,
                        OutboxEventRow.organization_id == organization_id,
                        OutboxEventRow.claim_owner == claim_owner,
                        OutboxEventRow.claim_expires_at > current,
                        OutboxEventRow.published_at.is_(None),
                    )
                    .values(
                        published_at=current,
                        claim_owner=None,
                        claim_expires_at=None,
                        last_error=None,
                        updated_at=current,
                    )
                )
                session.commit()
                return result.rowcount == 1
            except BaseException:
                session.rollback()
                raise

    def mark_publish_failed(
        self,
        outbox_id: UUID,
        organization_id: UUID,
        claim_owner: str,
        failed_at: datetime,
        error: str,
        retry_delay: timedelta,
    ) -> bool:
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay cannot be negative")
        with self._session_factory() as session:
            try:
                current = self._database_now(session, failed_at)
                result = session.execute(
                    update(OutboxEventRow)
                    .where(
                        OutboxEventRow.outbox_id == outbox_id,
                        OutboxEventRow.organization_id == organization_id,
                        OutboxEventRow.claim_owner == claim_owner,
                        OutboxEventRow.claim_expires_at > current,
                        OutboxEventRow.published_at.is_(None),
                    )
                    .values(
                        publish_attempts=OutboxEventRow.publish_attempts + 1,
                        available_at=current + retry_delay,
                        claim_owner=None,
                        claim_expires_at=None,
                        last_error=error,
                        updated_at=current,
                    )
                )
                session.commit()
                return result.rowcount == 1
            except BaseException:
                session.rollback()
                raise

    def _database_now(self, session: Session, fallback: datetime) -> datetime:
        if self._clock is not None:
            value = self._clock(session)
        else:
            value = session.scalar(select(func.now()))
        if value is None:
            value = fallback
        return _utc(value)


def _detached_event(
    row: OutboxEventRow, fallback_time: datetime, *, task_found: bool
) -> ClaimedOutboxEvent:
    occurred_at = row.created_at or fallback_time
    correlation_id = _payload_uuid(row.payload, "correlation_id")
    return ClaimedOutboxEvent(
        outbox_id=row.outbox_id,
        organization_id=row.organization_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        task_id=row.task_id,
        dispatch_seq=row.dispatch_seq,
        event_type=row.event_type,
        schema_version=row.schema_version,
        payload=dict(row.payload or {}),
        occurred_at=_utc(occurred_at),
        publish_attempts=row.publish_attempts,
        correlation_id=correlation_id,
        task_found=row.task_id is None or task_found,
    )


def _payload_uuid(payload: Any, key: str) -> UUID | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = ["SqlAlchemyOutboxRepository"]
