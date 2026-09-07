"""Ports for reading and publishing realtime inspection alerts."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from odp_schemas.events import InspectionAlert


@dataclass(frozen=True, slots=True)
class StoredInspectionAlert:
    cursor: str
    alert: InspectionAlert
    updated_at: datetime
    line_id: UUID | None


class InspectionAlertReadPort(Protocol):
    """Supplies persisted alerts to REST reconciliation and websocket delivery."""

    def list(
        self, organization_id: UUID, after_cursor: str | None, limit: int,
        authorized_line_ids: frozenset[UUID] | None = None,
    ) -> Sequence[StoredInspectionAlert]: ...

    def subscribe(self, after_cursor: str | None) -> AsyncIterator[StoredInspectionAlert]: ...


class InspectionAlertFeedPort(InspectionAlertReadPort, Protocol):
    """Backward-compatible local/demo feed that can also append a fact."""

    def publish(self, alert: InspectionAlert, line_id: UUID | None) -> str: ...


class InspectionAlertPublisherPort(Protocol):
    """Appends an inspection alert to a cross-instance notification transport."""

    def publish(self, alert: InspectionAlert, line_id: UUID | None) -> str: ...
