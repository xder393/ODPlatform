"""Tenant and lifecycle-authorized evidence URL endpoint."""

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.models import InspectionEventRow
from odp_api.adapters.persistence.task_models import FrameArtifactRow
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.tasks.models import ArtifactLifecycle, ArtifactState
from odp_api.ports.storage import ObjectStoragePort


class EvidenceUrlResponse(BaseModel):
    url: str
    expires_in: int


class EvidenceService:
    def __init__(self, sessions: sessionmaker[Session], storage: ObjectStoragePort | None) -> None:
        self._sessions = sessions
        self._storage = storage

    def presign(
        self,
        artifact_id: UUID,
        organization_id: UUID,
        line_ids: frozenset[UUID] | None,
    ) -> EvidenceUrlResponse | None:
        with self._sessions() as session:
            row = session.execute(
                select(FrameArtifactRow, InspectionEventRow.line_id)
                .join(
                    InspectionEventRow,
                    (InspectionEventRow.evidence_artifact_id == FrameArtifactRow.artifact_id)
                    & (InspectionEventRow.organization_id == FrameArtifactRow.organization_id),
                )
                .where(
                    FrameArtifactRow.artifact_id == artifact_id,
                    FrameArtifactRow.organization_id == organization_id,
                    FrameArtifactRow.state == ArtifactState.AVAILABLE.value,
                    FrameArtifactRow.lifecycle == ArtifactLifecycle.EVIDENCE.value,
                    FrameArtifactRow.object_key.is_not(None),
                )
            ).first()
            if row is None:
                return None
            artifact, line_id = row
            if line_ids is not None and line_id not in line_ids:
                return None
            if self._storage is None or artifact.object_key is None:
                raise RuntimeError("evidence storage is not configured")
            return EvidenceUrlResponse(
                url=self._storage.presign_get(artifact.object_key, expires_seconds=60),
                expires_in=60,
            )


def create_artifacts_router(
    service: EvidenceService,
    actor_provider: Callable[[], Actor] = get_current_actor,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])

    @router.get("/{artifact_id}/evidence-url", response_model=EvidenceUrlResponse)
    def evidence_url(
        artifact_id: UUID,
        actor: Actor = Depends(actor_provider),
    ) -> EvidenceUrlResponse:
        if Role(actor.role) is not Role.ADMINISTRATOR and not actor.line_ids:
            raise HTTPException(status_code=403, detail="No authorized production lines")
        try:
            result = service.presign(artifact_id, actor.organization_id, _line_scope(actor))
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail="Evidence storage is unavailable") from error
        if result is None:
            raise HTTPException(status_code=404, detail="Evidence artifact not found")
        return result

    return router


def _line_scope(actor: Actor) -> frozenset[UUID] | None:
    if Role(actor.role) is Role.ADMINISTRATOR:
        return None
    return actor.line_ids


__all__ = ["EvidenceService", "EvidenceUrlResponse", "create_artifacts_router"]
