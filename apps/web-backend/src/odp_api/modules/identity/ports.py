"""Persistence-facing identity contracts owned by the identity module."""

from typing import Protocol
from uuid import UUID

from odp_api.modules.identity.models import Actor


class ActorRepositoryPort(Protocol):
    def get(self, actor_id: UUID) -> Actor | None: ...

    def get_by_email(self, email: str) -> Actor | None: ...


class PasswordCredentialPort(Protocol):
    def password_hash(self, actor_id: UUID) -> str | None: ...
