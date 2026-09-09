"""RBAC and tenant-scoped inspection-session endpoints."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.policies import AuthorizationDenied, authorize
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.inspection_sessions.models import (
    InspectionSessionCreate,
    InspectionSessionListResponse,
    InspectionSessionResponse,
)
from odp_api.modules.inspection_sessions.service import (
    InspectionSessionService,
    SessionIdempotencyConflict,
)


def create_inspection_sessions_router(
    service: InspectionSessionService,
    actor_provider: Callable[[], Actor] = get_current_actor,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/inspection-sessions", tags=["inspection-sessions"])

    @router.post("", response_model=InspectionSessionResponse, status_code=status.HTTP_201_CREATED)
    def create_session(
        request: InspectionSessionCreate,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        actor: Actor = Depends(actor_provider),
    ) -> InspectionSessionResponse:
        if idempotency_key is None:
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        _authorize(actor, "inspection_session:start", actor.organization_id, request.line_id)
        try:
            return service.create(actor.organization_id, request, idempotency_key)
        except SessionIdempotencyConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("", response_model=InspectionSessionListResponse)
    def list_sessions(
        limit: int = Query(default=100), actor: Actor = Depends(actor_provider)
    ) -> InspectionSessionListResponse:
        _authorize_read(actor)
        try:
            return InspectionSessionListResponse(
                items=service.list(actor.organization_id, _authorized_lines(actor), limit)
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/{session_id}", response_model=InspectionSessionResponse)
    def get_session(session_id: UUID, actor: Actor = Depends(actor_provider)) -> InspectionSessionResponse:
        session = service.get(actor.organization_id, session_id)
        if session is None or not _line_visible(actor, session.line_id):
            raise HTTPException(status_code=404, detail="Inspection session not found")
        return session

    @router.post("/{session_id}:stop", response_model=InspectionSessionResponse)
    def stop_session(session_id: UUID, actor: Actor = Depends(actor_provider)) -> InspectionSessionResponse:
        session = service.get(actor.organization_id, session_id)
        if session is None or not _line_visible(actor, session.line_id):
            raise HTTPException(status_code=404, detail="Inspection session not found")
        _authorize(actor, "inspection_session:stop", actor.organization_id, session.line_id)
        result = service.request_stop(actor.organization_id, session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Inspection session not found")
        return result

    return router


def _authorize(actor: Actor, permission: str, organization_id: UUID, line_id: UUID | None) -> None:
    try:
        authorize(actor, permission, organization_id, line_id)
    except AuthorizationDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def _authorize_read(actor: Actor) -> None:
    if Role(actor.role) is Role.ADMINISTRATOR:
        return
    if not actor.line_ids:
        raise HTTPException(status_code=403, detail="No authorized production lines")


def _authorized_lines(actor: Actor) -> frozenset[UUID] | None:
    return None if Role(actor.role) is Role.ADMINISTRATOR else actor.line_ids


def _line_visible(actor: Actor, line_id: UUID | None) -> bool:
    return Role(actor.role) is Role.ADMINISTRATOR or line_id in actor.line_ids


__all__ = ["create_inspection_sessions_router"]
