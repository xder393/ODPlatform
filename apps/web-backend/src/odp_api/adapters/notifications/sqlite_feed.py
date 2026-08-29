"""Durable SQLite/SQLAlchemy inspection alert feed.

The condition is deliberately only a latency hint: every subscriber reads the
database again after a wake-up (and after a timeout), so facts remain usable
after process restarts or missed notifications.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from threading import Condition
from typing import Final
from uuid import UUID

from odp_schemas.events import InspectionAlert
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.models import InspectionAlertFeedRow
from odp_api.ports.notifications import StoredInspectionAlert

_MAX_LIMIT: Final = 100
_WAKE_TIMEOUT_SECONDS: Final = 1.0


class SqliteInspectionAlertFeed:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory
        self._condition = Condition()
        self._generation = 0

    def publish(self, alert: InspectionAlert, line_id: UUID | None) -> str:
        """Persist before any notification and deduplicate by the event identity."""
        try:
            with self._sessions.begin() as session:
                row = self._existing(session, alert.organization_id, alert.event_id)
                if row is None:
                    row = InspectionAlertFeedRow(
                        event_id=alert.event_id,
                        organization_id=alert.organization_id,
                        line_id=line_id,
                        payload=alert.model_dump(mode="json"),
                        created_at=_as_utc(alert.occurred_at),
                    )
                    session.add(row)
                    session.flush()
        except IntegrityError:
            with self._sessions() as session:
                row = self._existing(session, alert.organization_id, alert.event_id)
                if row is None:
                    raise
        with self._condition:
            self._generation += 1
            self._condition.notify_all()
        return str(row.cursor)

    def list(
        self,
        organization_id: UUID,
        after_cursor: str | None,
        limit: int,
        authorized_line_ids: frozenset[UUID] | None = None,
    ) -> list[StoredInspectionAlert]:
        if not 1 <= limit <= _MAX_LIMIT:
            raise ValueError("limit must be between 1 and 100")
        cursor = _parse_cursor(after_cursor)
        statement = select(InspectionAlertFeedRow).where(
            InspectionAlertFeedRow.organization_id == organization_id,
            InspectionAlertFeedRow.cursor > cursor,
        )
        if authorized_line_ids is not None:
            statement = statement.where(
                InspectionAlertFeedRow.line_id.in_(authorized_line_ids)
            )
        statement = statement.order_by(InspectionAlertFeedRow.cursor).limit(limit)
        with self._sessions() as session:
            return [_stored(row) for row in session.scalars(statement)]

    async def subscribe(
        self, after_cursor: str | None
    ) -> AsyncIterator[StoredInspectionAlert]:
        cursor = _parse_cursor(after_cursor)
        while True:
            # Deliberately unscoped here: routers enforce tenant and line scope
            # for each event. This also lets a single subscription serve actors
            # from different tenants without trusting a cursor as authorization.
            rows = self._list_after(cursor)
            for stored in rows:
                cursor = int(stored.cursor)
                yield stored
            generation = self._snapshot_generation()
            await asyncio.to_thread(self._wait_for_change, generation)

    def _list_after(self, cursor: int) -> list[StoredInspectionAlert]:
        statement = (
            select(InspectionAlertFeedRow)
            .where(InspectionAlertFeedRow.cursor > cursor)
            .order_by(InspectionAlertFeedRow.cursor)
        )
        with self._sessions() as session:
            return [_stored(row) for row in session.scalars(statement)]

    @staticmethod
    def _existing(
        session: Session, organization_id: UUID, event_id: UUID
    ) -> InspectionAlertFeedRow | None:
        return session.scalar(
            select(InspectionAlertFeedRow).where(
                InspectionAlertFeedRow.organization_id == organization_id,
                InspectionAlertFeedRow.event_id == event_id,
            )
        )

    def _snapshot_generation(self) -> int:
        with self._condition:
            return self._generation

    def _wait_for_change(self, generation: int) -> None:
        with self._condition:
            if self._generation == generation:
                self._condition.wait(timeout=_WAKE_TIMEOUT_SECONDS)


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None or cursor == "":
        return 0
    if not cursor.isdigit() or int(cursor) < 0:
        raise ValueError("invalid cursor")
    return int(cursor)


def _stored(row: InspectionAlertFeedRow) -> StoredInspectionAlert:
    return StoredInspectionAlert(
        cursor=str(row.cursor),
        alert=InspectionAlert.model_validate(row.payload),
        updated_at=_as_utc(row.created_at),
        line_id=row.line_id,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
