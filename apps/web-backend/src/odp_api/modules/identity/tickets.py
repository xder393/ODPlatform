"""Opaque, short-lived, single-use WebSocket ticket services."""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from odp_api.modules.identity.ports import WebSocketTicketStorePort

WEBSOCKET_TICKET_TTL_SECONDS = 60
_SYSTEM_ORGANIZATION_ID = UUID(int=0)


class InvalidWebSocketTicket(ValueError):
    """Raised without including ticket material when a ticket cannot be consumed."""


class WebSocketTicketService:
    def __init__(self, store: WebSocketTicketStorePort) -> None:
        self._store = store

    def issue(self, actor_id: UUID, now: datetime | None = None) -> str:
        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + timedelta(seconds=WEBSOCKET_TICKET_TTL_SECONDS)
        for _ in range(3):
            ticket = secrets.token_urlsafe(32)
            if self._store.issue(ticket, actor_id, expires_at):
                return ticket
        raise RuntimeError("Unable to issue WebSocket ticket")

    def consume(self, ticket: str, now: datetime | None = None) -> UUID:
        if not ticket:
            raise InvalidWebSocketTicket("Invalid WebSocket ticket")
        actor_id = self._store.consume(ticket, now or datetime.now(UTC))
        if actor_id is None:
            raise InvalidWebSocketTicket("Invalid WebSocket ticket")
        return actor_id
