"""Immutable records and canonical serialization for the audit hash chain."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

GENESIS_HASH = "0" * 64


def canonical_timestamp(value: datetime) -> str:
    """Return the one timestamp representation permitted in a chain hash."""
    if value.tzinfo is None:
        raise ValueError("Audit timestamps must be timezone-aware.")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class AuditCommand:
    """The complete, caller-supplied intent for one immutable audit entry."""

    organization_id: UUID
    resource_type: str
    resource_id: UUID
    action: str
    change_summary: str
    actor_id: UUID | None
    occurred_at: datetime
    correlation_id: UUID | None
    request_ip: str | None

    def __post_init__(self) -> None:
        canonical_timestamp(self.occurred_at)
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class AuditLog:
    """An append-only audit record whose hash commits to its predecessor."""

    audit_id: UUID
    organization_id: UUID
    sequence: int
    resource_type: str
    resource_id: UUID
    action: str
    change_summary: str
    actor_id: UUID | None
    occurred_at: datetime
    correlation_id: UUID | None
    request_ip: str | None
    previous_hash: str
    entry_hash: str


@dataclass(frozen=True, slots=True)
class AuditChainHead:
    """The one mutable record per organization; audit entries are never mutable."""

    organization_id: UUID
    last_sequence: int = 0
    head_hash: str = GENESIS_HASH


@dataclass(frozen=True, slots=True)
class AuditChainSnapshot:
    """Entries and head read together while the organization chain is locked."""

    entries: tuple[AuditLog, ...]
    head: AuditChainHead


@dataclass(frozen=True, slots=True)
class VerificationResult:
    organization_id: UUID
    is_valid: bool
    checked_entries: int
    failed_sequence: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AuditP0Failure:
    organization_id: UUID
    verification: VerificationResult


def canonical_hash_input(
    *,
    organization_id: UUID,
    sequence: int,
    resource_type: str,
    resource_id: UUID,
    action: str,
    change_summary: str,
    actor_id: UUID | None,
    occurred_at: datetime,
    correlation_id: UUID | None,
    request_ip: str | None,
    previous_hash: str,
) -> bytes:
    """Produce the versioned, byte-stable input committed by an audit hash.

    This exact JSON object is deliberately explicit: adding or renaming a field is
    a hash format change and must be introduced as a new version, not silently.
    """
    payload = {
        "action": action,
        "actor_id": str(actor_id) if actor_id is not None else None,
        "change_summary": change_summary,
        "correlation_id": str(correlation_id) if correlation_id is not None else None,
        "occurred_at": canonical_timestamp(occurred_at),
        "organization_id": str(organization_id),
        "previous_hash": previous_hash,
        "request_ip": request_ip,
        "resource_id": str(resource_id),
        "resource_type": resource_type,
        "sequence": sequence,
        "version": 1,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def calculate_entry_hash(
    *,
    organization_id: UUID,
    sequence: int,
    resource_type: str,
    resource_id: UUID,
    action: str,
    change_summary: str,
    actor_id: UUID | None,
    occurred_at: datetime,
    correlation_id: UUID | None,
    request_ip: str | None,
    previous_hash: str,
) -> str:
    return hashlib.sha256(
        canonical_hash_input(
            organization_id=organization_id,
            sequence=sequence,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            change_summary=change_summary,
            actor_id=actor_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            request_ip=request_ip,
            previous_hash=previous_hash,
        )
    ).hexdigest()


def audit_log_from_command(command: AuditCommand, *, sequence: int, previous_hash: str) -> AuditLog:
    return AuditLog(
        audit_id=uuid4(),
        organization_id=command.organization_id,
        sequence=sequence,
        resource_type=command.resource_type,
        resource_id=command.resource_id,
        action=command.action,
        change_summary=command.change_summary,
        actor_id=command.actor_id,
        occurred_at=command.occurred_at,
        correlation_id=command.correlation_id,
        request_ip=command.request_ip,
        previous_hash=previous_hash,
        entry_hash=calculate_entry_hash(
            organization_id=command.organization_id,
            sequence=sequence,
            resource_type=command.resource_type,
            resource_id=command.resource_id,
            action=command.action,
            change_summary=command.change_summary,
            actor_id=command.actor_id,
            occurred_at=command.occurred_at,
            correlation_id=command.correlation_id,
            request_ip=command.request_ip,
            previous_hash=previous_hash,
        ),
    )
