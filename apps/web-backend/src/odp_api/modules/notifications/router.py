"""Transport adapters for the process-local inspection alert feed."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, WebSocket

from odp_api.modules.identity.models import Actor
from odp_api.modules.identity.policies import AuthorizationDenied, authorize
from odp_api.modules.identity.service import (
    WebSocketAuthenticationError,
    get_current_actor,
    get_current_websocket_actor,
)
from odp_schemas.events import InspectionAlert


@dataclass(frozen=True, slots=True)
class StoredInspectionAlert:
    alert: InspectionAlert
    updated_at: datetime
    line_id: UUID | None


class InMemoryInspectionAlertRepository:
    """Deterministic alert source for the simulated inspection workflow."""

    def __init__(
        self,
        alerts: tuple[InspectionAlert, ...],
        line_ids: Mapping[UUID, UUID | None] | None = None,
    ) -> None:
        line_ids = line_ids or {}
        self._alerts = tuple(
            StoredInspectionAlert(
                alert=alert,
                updated_at=alert.occurred_at,
                line_id=line_ids.get(alert.event_id),
            )
            for alert in alerts
        )

    def list(
        self, organization_id: UUID, updated_after: datetime | None = None
    ) -> list[StoredInspectionAlert]:
        return [
            stored
            for stored in self._alerts
            if stored.alert.organization_id == organization_id
            and (updated_after is None or stored.updated_at > updated_after)
        ]


def create_notifications_router(
    repository: InMemoryInspectionAlertRepository,
    actor_provider: Callable[[], Actor] = get_current_actor,
) -> APIRouter:
    """Expose reconciliation and one-delivery websocket views of inspection alerts."""
    router = APIRouter(tags=["inspection-events"])

    @router.get("/api/v1/inspection-events", response_model=list[InspectionAlert])
    def list_inspection_events(
        updated_after: datetime | None = Query(default=None),
        actor: Actor = Depends(actor_provider),
    ) -> list[InspectionAlert]:
        if updated_after is not None and updated_after.tzinfo is None:
            updated_after = updated_after.replace(tzinfo=UTC)
        return [
            stored.alert
            for stored in repository.list(actor.organization_id, updated_after)
            if _is_authorized(actor, stored)
        ]

    @router.websocket("/ws/inspection-events")
    async def inspection_events_socket(websocket: WebSocket) -> None:
        try:
            actor = _websocket_actor(websocket, actor_provider)
        except WebSocketAuthenticationError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        # The server emits each available event once per connection.  Recovery is
        # intentionally REST-based; there is no delivery acknowledgement protocol.
        for stored in repository.list(actor.organization_id):
            if _is_authorized(actor, stored):
                await websocket.send_json(stored.alert.model_dump(mode="json"))

    return router


def _websocket_actor(websocket: WebSocket, actor_provider: Callable[[], Actor]) -> Actor:
    if actor_provider is not get_current_actor:
        return actor_provider()
    test_override = websocket.app.dependency_overrides.get(get_current_actor)
    if test_override is not None:
        return test_override()
    return get_current_websocket_actor(websocket)


def _is_authorized(actor: Actor, stored: StoredInspectionAlert) -> bool:
    try:
        authorize(
            actor,
            "inspection_event:read:own_line",
            stored.alert.organization_id,
            stored.line_id,
        )
    except AuthorizationDenied:
        return False
    return True
