"""Transport adapters for the process-local inspection alert feed."""

from collections.abc import Callable, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
from starlette.websockets import WebSocketDisconnect
from odp_api.modules.identity.models import Actor
from odp_api.modules.identity.policies import AuthorizationDenied, authorize
from odp_api.modules.identity.service import (
    WebSocketAuthenticationError,
    get_current_actor,
    get_current_websocket_actor,
)
from odp_api.ports.notifications import InspectionAlertFeedPort, StoredInspectionAlert

from odp_schemas.events import InspectionAlert


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
                cursor=str(index + 1),
                alert=alert,
                updated_at=alert.occurred_at,
                line_id=line_ids.get(alert.event_id),
            )
            for index, alert in enumerate(alerts)
        )

    def list(
        self, organization_id: UUID, after_cursor: str | None = None, limit: int = 100
    ) -> list[StoredInspectionAlert]:
        return [
            stored
            for stored in self._alerts
            if stored.alert.organization_id == organization_id
            and (after_cursor is None or int(stored.cursor) > int(after_cursor))
        ][:limit]

    def publish(self, alert: InspectionAlert, line_id: UUID | None) -> str:
        raise RuntimeError("In-memory notification repository is read-only")

    async def subscribe(self, after_cursor: str | None):
        if False:
            yield None


def create_notifications_router(
    repository: InspectionAlertFeedPort,
    actor_provider: Callable[[], Actor] = get_current_actor,
) -> APIRouter:
    """Expose cursor reconciliation and continuous ticket-authenticated delivery."""
    router = APIRouter(tags=["inspection-events"])

    @router.get("/api/v1/inspection-events")
    def list_inspection_events(
        after_cursor: str | None = Query(default=None),
        limit: int = Query(default=100),
        actor: Actor = Depends(actor_provider),
    ) -> dict[str, object]:
        try:
            stored = repository.list(actor.organization_id, after_cursor, limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid cursor or limit") from error
        items = [_envelope(item) for item in stored if _is_authorized(actor, item)]
        return {"items": items, "next_cursor": items[-1]["cursor"] if items else after_cursor}

    @router.websocket("/ws/inspection-events")
    async def inspection_events_socket(websocket: WebSocket) -> None:
        try:
            actor = _websocket_actor(websocket, actor_provider)
        except WebSocketAuthenticationError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        cursor = websocket.query_params.get("cursor")
        registry = getattr(websocket.app.state, "metric_registry", None)
        if registry is not None:
            registry.inc("websocket_reconnect_total")
            registry.add("websocket_active_connections", 1)
        try:
            subscription_cursor = cursor
            for stored in repository.list(actor.organization_id, cursor, 100):
                subscription_cursor = stored.cursor
                if _is_authorized(actor, stored):
                    await websocket.send_json(_envelope(stored))
                    if registry is not None:
                        registry.inc("websocket_reconciled_events_total")
            async for stored in repository.subscribe(subscription_cursor):
                if _is_authorized(actor, stored):
                    await websocket.send_json(_envelope(stored))
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            if registry is not None:
                registry.add("websocket_active_connections", -1)

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


def _envelope(stored: StoredInspectionAlert) -> dict[str, object]:
    return {"cursor": stored.cursor, "alert": stored.alert.model_dump(mode="json")}
