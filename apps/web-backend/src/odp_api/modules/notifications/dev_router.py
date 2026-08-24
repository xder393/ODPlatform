"""Non-production, authenticated inspection-event trigger for browser E2E."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, status
from odp_api.modules.identity.models import Actor
from odp_api.modules.identity.policies import AuthorizationDenied, authorize
from odp_api.modules.identity.service import get_current_actor
from odp_api.ports.notifications import InspectionAlertFeedPort
from pydantic import BaseModel

from odp_schemas.events import InspectionAlert


class DevInspectionEventRequest(BaseModel):
    event_id: UUID
    line_id: UUID


def create_development_notifications_router(repository: InspectionAlertFeedPort) -> APIRouter:
    """Expose a deterministic, actor-scoped event publisher outside production only."""
    router = APIRouter(prefix="/api/v1/dev", tags=["development"])

    @router.post("/inspection-events", status_code=status.HTTP_201_CREATED)
    def publish_inspection_event(
        request: DevInspectionEventRequest,
        actor: Annotated[Actor, Depends(get_current_actor)],
    ) -> dict[str, object]:
        try:
            authorize(actor, "inspection_event:read:own_line", actor.organization_id, request.line_id)
        except AuthorizationDenied as error:
            raise HTTPException(status_code=403, detail="Not authorized for this production line") from error
        alert = InspectionAlert(
            event_id=request.event_id,
            organization_id=actor.organization_id,
            camera_id=uuid5(request.event_id, "development-trigger-camera"),
            occurred_at=datetime(2026, 8, 24, tzinfo=UTC),
            defect_class="scratch",
            confidence=0.99,
        )
        cursor = repository.publish(alert, request.line_id)
        return {"cursor": cursor, "alert": alert.model_dump(mode="json")}

    return router
