"""Shared time and ownership predicates for ingestion session fencing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from odp_api.adapters.persistence.task_models import InspectionSessionRow
from odp_api.ports.inspection_sessions import IngestionClaim


def database_now(
    session: Session,
    clock: Callable[[Session], datetime] | None = None,
) -> datetime:
    """Return a UTC-aware database timestamp.

    PostgreSQL uses ``clock_timestamp`` so a long-running transaction does not
    reuse its transaction-start time.  SQLite's ``CURRENT_TIMESTAMP`` is UTC
    but is returned as a naive text value, so it is normalized below.  Tests
    may inject a callable without making production callers provide a clock.
    """

    if clock is not None:
        value = clock(session)
    elif session.get_bind().dialect.name == "postgresql":
        value = session.scalar(text("SELECT clock_timestamp()"))
    elif session.get_bind().dialect.name == "sqlite":
        value = session.scalar(text("SELECT CURRENT_TIMESTAMP"))
    else:
        value = session.scalar(select(func.now()))
    return _as_utc(value)


def owns_live_session(
    row: InspectionSessionRow,
    claim: IngestionClaim,
    now: datetime,
) -> bool:
    """Return whether ``claim`` still fences the session at ``now``."""

    expiry = row.lease_expires_at
    return (
        row.organization_id == claim.organization_id
        and row.camera_id == claim.camera_id
        and row.session_id == claim.session_id
        and row.owner_instance_id == claim.owner_instance_id
        and row.ingestion_generation == claim.generation
        and row.status == "RUNNING"
        and expiry is not None
        and _as_utc(expiry) > _as_utc(now)
    )


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        raise RuntimeError("database clock returned no timestamp")
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["database_now", "owns_live_session"]
