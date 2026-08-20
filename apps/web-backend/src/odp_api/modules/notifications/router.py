"""Transport adapters for the process-local inspection alert feed."""

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Query, WebSocket

from odp_schemas.events import InspectionAlert


@dataclass(frozen=True, slots=True)
class StoredInspectionAlert:
    alert: InspectionAlert
    updated_at: datetime


class InMemoryInspectionAlertRepository:
    """Deterministic alert source for the simulated inspection workflow."""

    def __init__(self, alerts: tuple[InspectionAlert, ...]) -> None:
        self._alerts = tuple(
            StoredInspectionAlert(alert=alert, updated_at=alert.occurred_at)
            for alert in alerts
        )

    def list(self, updated_after: datetime | None = None) -> list[InspectionAlert]:
        return [
            stored.alert
            for stored in self._alerts
            if updated_after is None or stored.updated_at > updated_after
        ]


def create_notifications_router(repository: InMemoryInspectionAlertRepository) -> APIRouter:
    """Expose reconciliation and one-delivery websocket views of inspection alerts."""
    router = APIRouter(tags=["inspection-events"])

    @router.get("/api/v1/inspection-events", response_model=list[InspectionAlert])
    def list_inspection_events(
        updated_after: datetime | None = Query(default=None),
    ) -> list[InspectionAlert]:
        if updated_after is not None and updated_after.tzinfo is None:
            updated_after = updated_after.replace(tzinfo=UTC)
        return repository.list(updated_after)

    @router.websocket("/ws/inspection-events")
    async def inspection_events_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        # The server emits each available event once per connection.  Recovery is
        # intentionally REST-based; there is no delivery acknowledgement protocol.
        for alert in repository.list():
            await websocket.send_json(alert.model_dump(mode="json"))

    return router
