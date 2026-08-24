"""Opaque, short-lived, single-use WebSocket ticket services."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import secrets
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.models import ReauthenticationMarkerRow, WebSocketTicketRow


WEBSOCKET_TICKET_TTL_SECONDS = 60
_SYSTEM_ORGANIZATION_ID = UUID(int=0)


class InvalidWebSocketTicket(ValueError):
    """Raised without including ticket material when a ticket cannot be consumed."""


class WebSocketTicketStorePort(Protocol):
    def issue(self, ticket: str, actor_id: UUID, expires_at: datetime) -> bool: ...

    def consume(self, ticket: str, now: datetime) -> UUID | None: ...


class SqliteWebSocketTicketStore:
    """Durable SQLite ticket store using conditional DELETE ... RETURNING."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def issue(self, ticket: str, actor_id: UUID, expires_at: datetime) -> bool:
        with self._session_factory.begin() as session:
            session.add(
                WebSocketTicketRow(
                    ticket_id=uuid4(),
                    organization_id=_SYSTEM_ORGANIZATION_ID,
                    actor_id=actor_id,
                    token_hash=_ticket_digest(ticket),
                    expires_at=expires_at,
                    consumed_at=None,
                )
            )
        return True

    def consume(self, ticket: str, now: datetime) -> UUID | None:
        with self._session_factory.begin() as session:
            statement = (
                delete(WebSocketTicketRow)
                .where(
                    WebSocketTicketRow.token_hash == _ticket_digest(ticket),
                    WebSocketTicketRow.expires_at > now,
                )
                .returning(WebSocketTicketRow.actor_id)
            )
            return session.scalar(statement)


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


def _ticket_digest(ticket: str) -> str:
    return sha256(ticket.encode()).hexdigest()


class SqliteReauthenticationStore:
    """Durable local equivalent of the Redis five-minute marker."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def set_last_reauth_at(self, actor_id: UUID, occurred_at: datetime) -> None:
        expires_at = occurred_at + timedelta(seconds=300)
        with self._session_factory.begin() as session:
            marker = session.get(ReauthenticationMarkerRow, actor_id)
            if marker is None:
                session.add(
                    ReauthenticationMarkerRow(
                        actor_id=actor_id, occurred_at=occurred_at, expires_at=expires_at
                    )
                )
            else:
                marker.occurred_at = occurred_at
                marker.expires_at = expires_at

    def get_last_reauth_at(self, actor_id: UUID, now: datetime) -> datetime | None:
        with self._session_factory.begin() as session:
            marker = session.scalar(
                select(ReauthenticationMarkerRow).where(
                    ReauthenticationMarkerRow.actor_id == actor_id,
                    ReauthenticationMarkerRow.expires_at >= now,
                )
            )
            if marker is None:
                session.execute(
                    delete(ReauthenticationMarkerRow).where(
                        ReauthenticationMarkerRow.actor_id == actor_id
                    )
                )
                return None
            return marker.occurred_at
