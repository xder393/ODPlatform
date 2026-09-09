"""Redis-backed stores for expiring credential-security state."""

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from odp_api.modules.identity.ports import (
    REAUTHENTICATION_TTL_SECONDS,
    WebSocketTicketStorePort,
)
from odp_api.modules.identity.tickets import WEBSOCKET_TICKET_TTL_SECONDS


class RedisSecurityClient(Protocol):
    def set_ex(self, key: str, value: str, seconds: int, *, nx: bool = False) -> bool: ...

    def get(self, key: str) -> str | None: ...

    def getdel(self, key: str) -> str | None: ...


class RedisReauthenticationStore:
    """Actor-scoped five-minute markers retained by Redis TTL."""

    def __init__(self, client: RedisSecurityClient) -> None:
        self._client = client

    def set_last_reauth_at(self, actor_id: UUID, occurred_at: datetime) -> None:
        self._client.set_ex(_reauth_key(actor_id), occurred_at.isoformat(), REAUTHENTICATION_TTL_SECONDS)

    def get_last_reauth_at(self, actor_id: UUID, now: datetime) -> datetime | None:
        value = self._client.get(_reauth_key(actor_id))
        if value is None:
            return None
        try:
            occurred_at = datetime.fromisoformat(value)
        except ValueError:
            return None
        return occurred_at if occurred_at + timedelta(seconds=REAUTHENTICATION_TTL_SECONDS) >= now else None


class RedisWebSocketTicketStore(WebSocketTicketStorePort):
    """Digest-keyed Redis ticket store using SET EX NX and atomic GETDEL."""

    def __init__(self, client: RedisSecurityClient) -> None:
        self._client = client

    def issue(self, ticket: str, actor_id: UUID, expires_at: datetime) -> bool:
        return self._client.set_ex(
            _ticket_key(ticket), str(actor_id), WEBSOCKET_TICKET_TTL_SECONDS, nx=True
        )

    def consume(self, ticket: str, now: datetime) -> UUID | None:
        value = self._client.getdel(_ticket_key(ticket))
        try:
            return UUID(value) if value is not None else None
        except ValueError:
            return None


def _ticket_key(ticket: str) -> str:
    return f"odp:ws-ticket:{sha256(ticket.encode()).hexdigest()}"


def _reauth_key(actor_id: UUID) -> str:
    return f"odp:reauth:{actor_id}"
