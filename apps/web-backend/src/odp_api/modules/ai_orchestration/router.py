"""Authenticated HTTP endpoint for cited inspection advice."""

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from odp_api.modules.ai_orchestration.service import AdviceResponse, AdviceService
from odp_api.modules.cases.ports import CaseRepositoryPort
from odp_api.modules.identity.models import Actor
from odp_api.modules.identity.policies import AuthorizationDenied, authorize
from odp_api.modules.identity.service import get_current_actor


def create_advice_router(
    repository: CaseRepositoryPort,
    advice_service: AdviceService,
    actor_provider: Callable[[], Actor] = get_current_actor,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/cases", tags=["advice"])

    @router.post("/{case_id}/advice", response_model=AdviceResponse)
    def get_advice(
        case_id: UUID,
        actor: Actor = Depends(actor_provider),
    ) -> AdviceResponse:
        stored_case = repository.get(case_id, actor.organization_id)
        if stored_case is None:
            raise HTTPException(status_code=404, detail="Case not found")
        try:
            authorize(
                actor,
                "defect_case:read:own_line",
                stored_case.case.organization_id,
                stored_case.case.line_id,
            )
        except AuthorizationDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return advice_service.advise(stored_case.case)

    return router
