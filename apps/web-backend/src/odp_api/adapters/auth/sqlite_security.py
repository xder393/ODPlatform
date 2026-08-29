"""SQLite implementations of durable credential-security stores."""

from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.models import ReauthenticationMarkerRow, WebSocketTicketRow
from odp_api.modules.identity.ports import REAUTHENTICATION_TTL_SECONDS
from odp_api.modules.identity.tickets import WEBSOCKET_TICKET_TTL_SECONDS


_SYSTEM_ORGANIZATION_ID = UUID(int=0)


class SqliteWebSocketTicketStore:
    """Consume tickets atomically with conditional SQLite DELETE ... RETURNING."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def issue(self, ticket: str, actor_id: UUID, expires_at: datetime) -> bool:
        issued_at = expires_at - timedelta(seconds=WEBSOCKET_TICKET_TTL_SECONDS)
        with self._session_factory.begin() as session:
            _delete_expired_tickets(session, issued_at)
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
            _delete_expired_tickets(session, now)
            statement = (
                delete(WebSocketTicketRow)
                .where(
                    WebSocketTicketRow.token_hash == _ticket_digest(ticket),
                    WebSocketTicketRow.expires_at > now,
                )
                .returning(WebSocketTicketRow.actor_id)
            )
            return session.scalar(statement)


class SqliteReauthenticationStore:
    """Durable local equivalent of the Redis five-minute marker."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def set_last_reauth_at(self, actor_id: UUID, occurred_at: datetime) -> None:
        expires_at = occurred_at + timedelta(seconds=REAUTHENTICATION_TTL_SECONDS)
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


def _delete_expired_tickets(session: Session, now: datetime) -> None:
    session.execute(delete(WebSocketTicketRow).where(WebSocketTicketRow.expires_at <= now))


def _ticket_digest(ticket: str) -> str:
    return sha256(ticket.encode()).hexdigest()
