"""Persistence boundary for database-owned inspection source sessions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class InspectionSession:
    session_id: UUID
    organization_id: UUID
    camera_id: UUID
    line_id: UUID
    source_type: str
    sanitized_uri: str
    secret_reference: str | None
    status: str


@dataclass(frozen=True, slots=True)
class IngestionClaim:
    organization_id: UUID
    camera_id: UUID
    session_id: UUID
    owner_instance_id: UUID
    generation: int


@dataclass(frozen=True, slots=True)
class ClaimedInspectionSession:
    session: InspectionSession
    claim: IngestionClaim
    initial_sequence: int


class InspectionSessionPort(Protocol):
    def claim_start_requests(self, process_id: str, limit: int) -> Sequence[InspectionSession]: ...

    def heartbeat(self, session_id: UUID, process_id: str, now: datetime) -> bool: ...

    def claim_stop_requests(self, process_id: str, limit: int) -> Sequence[InspectionSession]: ...

    def mark_failed(
        self, session_id: UUID, process_id: str, error_code: str, detail: str, now: datetime
    ) -> bool: ...


__all__ = [
    "ClaimedInspectionSession",
    "IngestionClaim",
    "InspectionSession",
    "InspectionSessionPort",
]
