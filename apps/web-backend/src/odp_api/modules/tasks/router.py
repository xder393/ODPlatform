"""Task diagnostics and auditable dead-letter replay endpoints."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.policies import AuthorizationDenied, authorize
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.tasks.diagnostics import (
    TaskDiagnostic,
    TaskDiagnosticListResponse,
    TaskDiagnosticsService,
    TaskReplayResponse,
)
from odp_api.ports.tasks import AdmissionRejected


def create_task_diagnostics_router(
    diagnostics: TaskDiagnosticsService,
    control: SqlAlchemyTaskControlRepository,
    actor_provider: Callable[[], Actor] = get_current_actor,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/inference-tasks", tags=["inference-tasks"])

    @router.get("", response_model=TaskDiagnosticListResponse)
    def list_tasks(
        limit: int = Query(default=100),
        task_status: Annotated[str | None, Query(alias="status")] = None,
        camera_id: UUID | None = None,
        actor: Actor = Depends(actor_provider),
    ) -> TaskDiagnosticListResponse:
        _require_read_scope(actor)
        try:
            items = diagnostics.list(
                actor.organization_id,
                _line_scope(actor),
                limit=limit,
                status=task_status,
                camera_id=camera_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return TaskDiagnosticListResponse(items=items)

    @router.get("/{task_id}", response_model=TaskDiagnostic)
    def get_task(task_id: UUID, actor: Actor = Depends(actor_provider)) -> TaskDiagnostic:
        _require_read_scope(actor)
        try:
            task = diagnostics.get(actor.organization_id, task_id, _line_scope(actor))
        except LookupError:
            task = None
        if task is None:
            raise HTTPException(status_code=404, detail="Inference task not found")
        return task

    @router.post("/{task_id}:replay", response_model=TaskReplayResponse, status_code=status.HTTP_201_CREATED)
    def replay_task(task_id: UUID, actor: Actor = Depends(actor_provider)) -> TaskReplayResponse:
        source = diagnostics.get(actor.organization_id, task_id, _line_scope(actor))
        if source is None or source.status != "DEAD_LETTER":
            raise HTTPException(status_code=404, detail="Dead-letter task not found")
        _require_permission(actor, "inference_task:replay", actor.organization_id, source.line_id)
        try:
            result = control.replay_dead_letter(task_id, actor.organization_id, actor.actor_id)
        except AdmissionRejected as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return TaskReplayResponse(
            task_id=result.task_id,
            source_task_id=task_id,
            status="READY",
            dispatch_seq=result.dispatch_seq,
        )

    return router


def _require_read_scope(actor: Actor) -> None:
    if Role(actor.role) is Role.ADMINISTRATOR:
        return
    _require_permission(actor, "inference_task:read:own_line", actor.organization_id, None)


def _require_permission(actor: Actor, permission: str, organization_id: UUID, line_id: UUID | None) -> None:
    try:
        if line_id is None and Role(actor.role) is not Role.ADMINISTRATOR and permission.endswith("own_line"):
            if not actor.line_ids:
                raise AuthorizationDenied("No authorized production lines")
            # Resource-level scope is enforced after loading the task.
            return
        authorize(actor, permission, organization_id, line_id)
    except AuthorizationDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def _line_scope(actor: Actor) -> frozenset[UUID] | None:
    return None if Role(actor.role) is Role.ADMINISTRATOR else actor.line_ids


__all__ = ["create_task_diagnostics_router"]
