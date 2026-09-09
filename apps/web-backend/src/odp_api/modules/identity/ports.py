"""Persistence-facing identity contracts owned by the identity module."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from odp_api.modules.identity.models import Actor

REAUTHENTICATION_TTL_SECONDS = 300


class ActorRepositoryPort(Protocol):
    def get(self, actor_id: UUID) -> Actor | None: ...

    def get_by_email(self, email: str) -> Actor | None: ...


class PasswordCredentialPort(Protocol):
    def password_hash(self, actor_id: UUID) -> str | None: ...


class ReauthenticationStorePort(Protocol):
    def set_last_reauth_at(self, actor_id: UUID, occurred_at: datetime) -> None: ...

    def get_last_reauth_at(self, actor_id: UUID, now: datetime) -> datetime | None: ...


class WebSocketTicketStorePort(Protocol):
    def issue(self, ticket: str, actor_id: UUID, expires_at: datetime) -> bool: ...

    def consume(self, ticket: str, now: datetime) -> UUID | None: ...
