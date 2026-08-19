from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from odp_api.modules.cases.errors import InvalidCaseTransition
from odp_api.modules.cases.service import CaseService
from odp_api.modules.inspection.models import CaseStatus, DefectCase, InspectionEvent


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
        )


@dataclass(slots=True)
class StoredCase:
    case: DefectCase
    updated_at: datetime


class InMemoryCaseRepository:
    """Process-local case storage for the deterministic demo fixture."""

    def __init__(self, initial_cases: tuple[DefectCase, ...]) -> None:
        now = datetime.now(UTC)
        self._cases = {case.case_id: StoredCase(case, now) for case in initial_cases}

    def list(self, updated_after: datetime | None) -> list[StoredCase]:
        return sorted(
            (
                stored_case
                for stored_case in self._cases.values()
                if updated_after is None or stored_case.updated_at > updated_after
            ),
            key=lambda stored_case: stored_case.updated_at,
            reverse=True,
        )

    def get(self, case_id: UUID) -> StoredCase | None:
        return self._cases.get(case_id)

    def save(self, case: DefectCase) -> StoredCase:
        stored_case = StoredCase(case=case, updated_at=datetime.now(UTC))
        self._cases[case.case_id] = stored_case
        return stored_case


def create_cases_router(repository: InMemoryCaseRepository) -> APIRouter:
    router = APIRouter(prefix="/api/v1/cases", tags=["cases"])

    @router.get("", response_model=list[CaseSummary])
    def list_cases(updated_after: datetime | None = Query(default=None)) -> list[CaseSummary]:
        return [CaseSummary.from_case(case) for case in repository.list(updated_after)]

    @router.post("/{case_id}/transitions", response_model=CaseSummary)
    def transition_case(case_id: UUID, request: CaseTransitionRequest) -> CaseSummary:
        stored_case = repository.get(case_id)
        if stored_case is None:
            raise HTTPException(status_code=404, detail="Case not found")
        try:
            transitioned = CaseService.transition(stored_case.case, request.status, uuid4())
        except InvalidCaseTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return CaseSummary.from_case(repository.save(transitioned))

    return router
