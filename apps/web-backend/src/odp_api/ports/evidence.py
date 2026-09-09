"""Resource-level authorization boundary for evidence artifacts."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class EvidenceAuthorizationPort(Protocol):
    """Authorize one tenant-scoped evidence read or raise a domain error."""

    def authorize(self, actor_id: UUID, organization_id: UUID, artifact_id: UUID) -> None: ...


__all__ = ["EvidenceAuthorizationPort"]
