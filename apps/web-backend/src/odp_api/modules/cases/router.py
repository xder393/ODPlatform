from __future__ import annotations

from datetime import UTC, datetime
import json
from threading import RLock
from typing import TYPE_CHECKING, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from odp_api.modules.cases.errors import InvalidCaseTransition
from odp_api.modules.cases.application import AuditContext, CaseApplicationService
from odp_api.modules.cases.ports import CaseTransition, CaseRepositoryPort, StoredCase
from odp_api.modules.cases.service import CaseService
from odp_api.modules.identity.models import Actor
from odp_api.modules.identity.policies import AuthorizationDenied, authorize
from odp_api.modules.identity.service import (
    InMemoryReauthenticationStore,
    ReauthenticationService,
    RecentReauthenticationRequired,
    get_current_actor,
)
from odp_api.modules.inspection.models import CaseStatus, DefectCase, InspectionEvent
from odp_api.observability.metrics import DEFAULT_REGISTRY, MetricRegistry
from odp_api.observability.tracing import get_correlation_id

if TYPE_CHECKING:
    from odp_api.modules.audit.models import AuditCommand
    from odp_api.modules.audit.service import AuditService


class CaseTransitionRequest(BaseModel):
    status: Literal["PENDING_CONFIRMATION", "IN_REVIEW", "RESOLVED", "FALSE_POSITIVE"]


class InspectionEventSummary(BaseModel):
    defect_class: str
    confidence: float
    model_release: str
    preprocessing_parameters: dict[str, str]
    threshold: float
    input_frame_sha256: str

    @classmethod
    def from_event(cls, event: InspectionEvent) -> "InspectionEventSummary":
        return cls(
            defect_class=event.defect_class,
            confidence=event.confidence,
            model_release=event.model_release,
            preprocessing_parameters=dict(event.preprocessing_parameters),
            threshold=event.threshold,
            input_frame_sha256=event.input_frame_sha256,
        )


class CaseSummary(BaseModel):
    case_id: UUID
    status: CaseStatus
    updated_at: datetime
    inspection_events: list[InspectionEventSummary]
    history: list["CaseTransitionSummary"]

    @classmethod
    def from_case(cls, stored_case: "StoredCase") -> "CaseSummary":
        return cls(
            case_id=stored_case.case.case_id,
            status=stored_case.case.status,
            updated_at=stored_case.updated_at,
            inspection_events=[
                InspectionEventSummary.from_event(event)
                for event in stored_case.case.inspection_events
            ],
            history=[CaseTransitionSummary.from_transition(transition) for transition in stored_case.history],
        )


class CaseTransitionSummary(BaseModel):
    from_status: CaseStatus
    to_status: CaseStatus
    actor_id: UUID | None
    occurred_at: datetime
    correlation_id: UUID | None

    @classmethod
    def from_transition(cls, transition: CaseTransition) -> "CaseTransitionSummary":
        return cls(
            from_status=transition.from_status,
            to_status=transition.to_status,
            actor_id=transition.actor_id,
            occurred_at=transition.occurred_at,
            correlation_id=transition.correlation_id,
        )


class InMemoryCaseRepository:
    """Process-local case storage for the deterministic demo fixture."""

    def __init__(self, initial_cases: tuple[DefectCase, ...]) -> None:
        now = datetime.now(UTC)
        self._cases = {case.case_id: StoredCase(case, now) for case in initial_cases}
        self._lock = RLock()

    def list(
        self, organization_id: UUID, updated_after: datetime | None
    ) -> list[StoredCase]:
        with self._lock:
            return sorted(
                (
                    stored_case
                    for stored_case in self._cases.values()
                    if stored_case.case.organization_id == organization_id
                    and (updated_after is None or stored_case.updated_at > updated_after)
                ),
                key=lambda stored_case: stored_case.updated_at,
                reverse=True,
            )

    def get(self, case_id: UUID, organization_id: UUID) -> StoredCase | None:
        with self._lock:
            stored_case = self._cases.get(case_id)
            if stored_case is None or stored_case.case.organization_id != organization_id:
                return None
            return stored_case

    def save(self, case: DefectCase) -> StoredCase:
        with self._lock:
            return self._save_unlocked(case)

    def save_with_audit(
        self, case: DefectCase, audit_service: "AuditService", audit_command: "AuditCommand"
    ) -> StoredCase:
        """Commit the in-memory case mutation only after its audit append succeeds.

        A database-backed repository implements this boundary with the case write
        and audit head-lock append in the same SQL transaction.
        """
        with self._lock:
            audit_service.append(audit_command)
            return self._save_unlocked(case)

    def transition_with_audit(
        self,
        case_id: UUID,
        organization_id: UUID,
        to_status: CaseStatus,
        actor: Actor,
        audit_service: "AuditService",
        make_audit_command: Callable[[DefectCase, DefectCase], "AuditCommand"],
    ) -> StoredCase | None:
        """Lock/load/transition/audit/save as one in-memory mutation transaction.

        The production persistence adapter implements this port using one database
        transaction that locks the case row and the organization's audit-chain head.
        """
        with self._lock:
            stored_case = self._cases.get(case_id)
            if stored_case is None or stored_case.case.organization_id != organization_id:
                return None
            transitioned = CaseService.transition(stored_case.case, to_status, actor)
            audit_command = make_audit_command(stored_case.case, transitioned)
            audit_service.append(audit_command)
            history = [
                *stored_case.history,
                CaseTransition(
                    from_status=stored_case.case.status,
                    to_status=transitioned.status,
                    actor_id=actor.actor_id,
                    occurred_at=audit_command.occurred_at,
                    correlation_id=audit_command.correlation_id,
                ),
            ]
            return self._save_unlocked(transitioned, history)

    def _save_unlocked(
        self, case: DefectCase, history: list[CaseTransition] | None = None
    ) -> StoredCase:
        stored_case = StoredCase(
            case=case,
            updated_at=datetime.now(UTC),
            history=(
                history
                if history is not None
                else self._cases.get(case.case_id, StoredCase(case, datetime.now(UTC))).history
            ),
        )
        self._cases[case.case_id] = stored_case
        return stored_case


def create_cases_router(
    repository: CaseRepositoryPort,
    actor_provider: Callable[[], Actor] = get_current_actor,
    reauthentication_service: ReauthenticationService | None = None,
    audit_service: "AuditService | None" = None,
    case_application_service: CaseApplicationService | None = None,
    metric_registry: MetricRegistry | None = None,
) -> APIRouter:
    from odp_api.modules.audit.service import AuditAppendBlocked, AuditService, InMemoryAuditRepository

    router = APIRouter(prefix="/api/v1/cases", tags=["cases"])
    reauth_service = reauthentication_service or ReauthenticationService(
        InMemoryReauthenticationStore()
    )
    case_audit_service = audit_service or AuditService(InMemoryAuditRepository())
    case_metrics = metric_registry or DEFAULT_REGISTRY

    @router.get("", response_model=list[CaseSummary])
    def list_cases(
        updated_after: datetime | None = Query(default=None),
        actor: Actor = Depends(actor_provider),
    ) -> list[CaseSummary]:
        if updated_after is not None and updated_after.tzinfo is None:
            updated_after = updated_after.replace(tzinfo=UTC)
        return [
            CaseSummary.from_case(case)
            for case in repository.list(actor.organization_id, updated_after)
            if _is_authorized(actor, "defect_case:read:own_line", case.case)
        ]

    @router.post("/{case_id}/transitions", response_model=CaseSummary)
    def transition_case(
        case_id: UUID,
        request: CaseTransitionRequest,
        http_request: Request,
        actor: Actor = Depends(actor_provider),
    ) -> CaseSummary:
        try:
            if case_application_service is not None:
                stored_transition = case_application_service.transition(
                    case_id,
                    request.status,
                    actor,
                    _audit_context(http_request),
                )
            else:
                # Explicit in-memory injection remains a compact unit-test adapter.
                stored_transition = repository.transition_with_audit(  # type: ignore[attr-defined]
                    case_id,
                    actor.organization_id,
                    request.status,
                    actor,
                    case_audit_service,
                    lambda before, after: _transition_audit_command(before, after, actor, http_request),
                )
        except AuthorizationDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except InvalidCaseTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except AuditAppendBlocked as error:
            raise HTTPException(status_code=503, detail="Audit chain verification recovery is required") from error
        if stored_transition is None:
            raise HTTPException(status_code=404, detail="Case not found")
        if request.status == "RESOLVED":
            _observe_case_resolution(stored_transition, case_metrics)
        return CaseSummary.from_case(stored_transition)

    @router.post("/{case_id}/pause")
    def simulate_pause(
        case_id: UUID,
        http_request: Request,
        actor: Actor = Depends(actor_provider),
    ) -> dict[str, str]:
        stored_case = repository.get(case_id, actor.organization_id)
        if stored_case is None:
            raise HTTPException(status_code=404, detail="Case not found")
        _require_authorized(actor, "defect_case:pause:own_line", stored_case.case)
        try:
            reauth_service.require_recent_reauth(actor.actor_id, datetime.now(UTC))
        except RecentReauthenticationRequired as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        try:
            case_audit_service.append(
                _pause_audit_command(stored_case.case, actor, http_request)
            )
        except AuditAppendBlocked as error:
            raise HTTPException(status_code=503, detail="Audit chain verification recovery is required") from error
        return {"status": "SIMULATED", "message": "No production line was paused."}

    return router


def _is_authorized(actor: Actor, permission: str, defect_case: DefectCase) -> bool:
    try:
        authorize(actor, permission, defect_case.organization_id, defect_case.line_id)
    except AuthorizationDenied:
        return False
    return True


def _require_authorized(actor: Actor, permission: str, defect_case: DefectCase) -> None:
    try:
        authorize(actor, permission, defect_case.organization_id, defect_case.line_id)
    except AuthorizationDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def _observe_case_resolution(stored_case: StoredCase, registry: MetricRegistry) -> None:
    """Record the case creation-to-RESOLVED duration in seconds.

    Case creation time is approximated by the first inspection event timestamp,
    and the resolution time by the repository write that stored the transition.
    """
    first_event = stored_case.case.inspection_events[0]
    resolution_seconds = (stored_case.updated_at - first_event.occurred_at).total_seconds()
    registry.observe("case_resolution_seconds", max(resolution_seconds, 0.0))


def _transition_audit_command(
    before: DefectCase, after: DefectCase, actor: Actor, request: Request | None
) -> "AuditCommand":
    from odp_api.modules.audit.models import AuditCommand

    context = _audit_context(request)
    return AuditCommand(
        organization_id=after.organization_id,
        resource_type="defect_case",
        resource_id=after.case_id,
        action="defect_case.transition",
        change_summary=json.dumps(
            {"from_status": before.status, "to_status": after.status},
            sort_keys=True,
            separators=(",", ":"),
        ),
        actor_id=actor.actor_id,
        occurred_at=context.occurred_at,
        correlation_id=context.correlation_id,
        request_ip=request.client.host if request is not None and request.client is not None else None,
    )


def _audit_context(request: Request | None) -> AuditContext:
    correlation_id = None
    raw_correlation_id = get_correlation_id()
    if raw_correlation_id:
        try:
            correlation_id = UUID(raw_correlation_id)
        except ValueError:
            correlation_id = None
    return AuditContext(
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        request_ip=request.client.host if request is not None and request.client is not None else None,
    )


def _pause_audit_command(case: DefectCase, actor: Actor, request: Request) -> "AuditCommand":
    from odp_api.modules.audit.models import AuditCommand

    context = _audit_context(request)
    return AuditCommand(
        organization_id=case.organization_id,
        resource_type="defect_case",
        resource_id=case.case_id,
        action="defect_case.pause.simulated",
        change_summary=json.dumps({"status": "SIMULATED"}, sort_keys=True, separators=(",", ":")),
        actor_id=actor.actor_id,
        occurred_at=context.occurred_at,
        correlation_id=context.correlation_id,
        request_ip=context.request_ip,
    )
