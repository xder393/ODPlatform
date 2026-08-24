from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    """Roles supported by the deterministic inspection demo."""

    INSPECTOR = "INSPECTOR"
    SUPERVISOR = "SUPERVISOR"
    ADMINISTRATOR = "ADMINISTRATOR"


@dataclass(frozen=True, slots=True)
class Actor:
    """Authenticated subject and the organization/line scope it may access."""

    actor_id: UUID
    organization_id: UUID
    role: Role
    line_ids: frozenset[UUID]
    # Optional because service accounts created before the login feature exist
    # without an email; the demo seed always sets one.
    email: str | None = None
