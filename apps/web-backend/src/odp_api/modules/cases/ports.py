"""Persistence-neutral case, history, and transaction boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Self
from uuid import UUID

from odp_api.modules.audit.service import AuditRepository
from odp_api.modules.inspection.models import DefectCase


@dataclass(frozen=True, slots=True)
class CaseTransition:
    from_status: str
    to_status: str
    actor_id: UUID | None
    occurred_at: datetime
    correlation_id: UUID | None


@dataclass(slots=True)
class StoredCase:
    case: DefectCase
    updated_at: datetime
    history: list[CaseTransition] = field(default_factory=list)


class CaseRepositoryPort(Protocol):
    def list(self, organization_id: UUID, updated_after: datetime | None) -> list[StoredCase]: ...

    def get(self, case_id: UUID, organization_id: UUID) -> StoredCase | None: ...

    def history(self, case_id: UUID, organization_id: UUID) -> list[CaseTransition]: ...


class MutableCaseRepositoryPort(CaseRepositoryPort, Protocol):
    def save_transition(
        self,
        before: DefectCase,
        after: DefectCase,
        actor_id: UUID,
        occurred_at: datetime,
        correlation_id: UUID | None,
    ) -> StoredCase: ...


class BusinessUnitOfWorkPort(Protocol):
    cases: MutableCaseRepositoryPort
    audits: AuditRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...

    def commit(self) -> None: ...
