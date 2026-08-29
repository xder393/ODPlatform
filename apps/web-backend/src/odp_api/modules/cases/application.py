"""Authorized durable case mutations coordinated through one unit of work."""

import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from odp_api.modules.audit.models import AuditCommand, audit_log_from_command
from odp_api.modules.audit.service import AuditService
from odp_api.modules.cases.ports import BusinessUnitOfWorkPort, StoredCase
from odp_api.modules.cases.service import CaseService
from odp_api.modules.identity.models import Actor
from odp_api.modules.inspection.models import CaseStatus


@dataclass(frozen=True, slots=True)
class AuditContext:
    occurred_at: datetime
    correlation_id: UUID | None
    request_ip: str | None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("Audit context timestamps must be timezone-aware.")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))


class CaseApplicationService:
    """Mutate a case, append its history and audit fact in one transaction."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], BusinessUnitOfWorkPort],
        audit_service: AuditService | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._audit_service = audit_service

    def transition(
        self,
        case_id: UUID,
        to_status: CaseStatus,
        actor: Actor,
        audit_context: AuditContext,
    ) -> StoredCase | None:
        guard = (
            self._audit_service.append_guard(actor.organization_id)
            if self._audit_service is not None
            else nullcontext()
        )
        # The guard is intentionally outermost: verification/recovery and all
        # writers take health before database locks, preventing lock inversion.
        with guard, self._unit_of_work_factory() as unit_of_work:
            before = unit_of_work.cases.get(case_id, actor.organization_id)
            if before is None:
                return None
            after = CaseService.transition(before.case, to_status, actor)
            stored = unit_of_work.cases.save_transition(
                before.case,
                after,
                actor.actor_id,
                audit_context.occurred_at,
                audit_context.correlation_id,
            )
            command = AuditCommand(
                organization_id=after.organization_id,
                resource_type="defect_case",
                resource_id=after.case_id,
                action="defect_case.transition",
                change_summary=json.dumps(
                    {"from_status": before.case.status, "to_status": after.status},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_id=actor.actor_id,
                occurred_at=audit_context.occurred_at,
                correlation_id=audit_context.correlation_id,
                request_ip=audit_context.request_ip,
            )
            unit_of_work.audits.append_under_head_lock(
                command,
                lambda sequence, previous_hash: audit_log_from_command(
                    command, sequence=sequence, previous_hash=previous_hash
                ),
            )
            unit_of_work.commit()
            return stored
