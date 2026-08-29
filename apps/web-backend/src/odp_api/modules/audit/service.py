"""Append-only audit service and an in-memory transactional adapter for tests."""

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from threading import Event, Lock, RLock
from typing import Callable, Protocol
from uuid import UUID

from odp_api.modules.audit.models import (
    AuditChainHead,
    AuditChainSnapshot,
    AuditCommand,
    AuditLog,
    VerificationResult,
    audit_log_from_command,
)
from odp_api.modules.identity.models import Actor
from odp_api.modules.identity.policies import authorize


class AuditAppendBlocked(RuntimeError):
    """Raised while a failed verification puts an organization in P0 hold."""


class AuditWriteError(RuntimeError):
    """Raised by an audit persistence adapter when an append cannot be stored."""


class AuditRecoveryRejected(RuntimeError):
    """Raised when recovery is requested before the organization chain is valid."""


class AuditRepository(Protocol):
    """Persistence boundary for the one locked-head append transaction.

    A PostgreSQL adapter must lock the organization chain-head row with
    ``SELECT ... FOR UPDATE``, insert the audit entry, and advance the head in
    the same database transaction.  The application database role receives no
    UPDATE or DELETE privilege on the immutable audit-entry table.
    """

    def append_under_head_lock(
        self, command: AuditCommand, make_entry: Callable[[int, str], AuditLog]
    ) -> AuditLog: ...

    def read_consistent_chain(self, organization_id: UUID) -> AuditChainSnapshot: ...

    def organization_ids(self) -> tuple[UUID, ...]: ...


class InMemoryAuditRepository:
    """Thread-safe adapter that models the production locked-head transaction."""

    def __init__(self) -> None:
        self._heads: dict[UUID, AuditChainHead] = {}
        self._entries: dict[UUID, list[AuditLog]] = defaultdict(list)
        self._organization_locks: dict[UUID, RLock] = {}
        self._locks_guard = Lock()
        self._snapshot_started: Event | None = None
        self._release_snapshot: Event | None = None

    def append_under_head_lock(
        self, command: AuditCommand, make_entry: Callable[[int, str], AuditLog]
    ) -> AuditLog:
        with self._lock_for(command.organization_id):
            head = self._heads.get(command.organization_id, AuditChainHead(command.organization_id))
            entry = make_entry(head.last_sequence + 1, head.head_hash)
            self._entries[command.organization_id].append(entry)
            self._heads[command.organization_id] = replace(
                head, last_sequence=entry.sequence, head_hash=entry.entry_hash
            )
            return entry

    def entries_for_organization(self, organization_id: UUID) -> tuple[AuditLog, ...]:
        with self._lock_for(organization_id):
            return tuple(self._entries[organization_id])

    def chain_head_for_organization(self, organization_id: UUID) -> AuditChainHead:
        with self._lock_for(organization_id):
            return self._heads.get(organization_id, AuditChainHead(organization_id))

    def read_consistent_chain(self, organization_id: UUID) -> AuditChainSnapshot:
        """Read entries and head under exactly one organization lock."""
        with self._lock_for(organization_id):
            if self._snapshot_started is not None and self._release_snapshot is not None:
                self._snapshot_started.set()
                if not self._release_snapshot.wait(timeout=5):
                    raise TimeoutError("Test snapshot pause was not released.")
                self._snapshot_started = None
                self._release_snapshot = None
            return AuditChainSnapshot(
                entries=tuple(self._entries[organization_id]),
                head=self._heads.get(organization_id, AuditChainHead(organization_id)),
            )

    def organization_ids(self) -> tuple[UUID, ...]:
        with self._locks_guard:
            return tuple(self._heads)

    def unsafe_replace_change_summary_for_test(self, audit_id: UUID, value: str) -> None:
        """Deliberately bypass immutability only to prove tamper detection in tests."""
        for organization_id in self.organization_ids():
            with self._lock_for(organization_id):
                entries = self._entries[organization_id]
                for index, entry in enumerate(entries):
                    if entry.audit_id == audit_id:
                        entries[index] = replace(entry, change_summary=value)
                        return
        raise KeyError(audit_id)

    def pause_consistent_snapshot_for_test(self, started: Event, release: Event) -> None:
        """Inject an append interleaving point while the chain lock remains held."""
        with self._locks_guard:
            self._snapshot_started = started
            self._release_snapshot = release

    def _lock_for(self, organization_id: UUID) -> RLock:
        with self._locks_guard:
            return self._organization_locks.setdefault(organization_id, RLock())


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self.repository = repository
        self._blocked_organizations: set[UUID] = set()
        self._health_lock = RLock()

    def append(self, command: AuditCommand) -> AuditLog:
        with self.append_guard(command.organization_id):
            return self.repository.append_under_head_lock(
                command,
                lambda sequence, previous_hash: audit_log_from_command(
                    command, sequence=sequence, previous_hash=previous_hash
                ),
            )

    @contextmanager
    def append_guard(self, organization_id: UUID):
        """Keep P0 health state stable through the complete append transaction."""
        with self._health_lock:
            if organization_id in self._blocked_organizations:
                raise AuditAppendBlocked(
                    f"Audit appends for organization {organization_id} are blocked pending explicit recovery."
                )
            yield

    def ensure_append_allowed(self, organization_id: UUID) -> None:
        with self.append_guard(organization_id):
            return None

    def verify_organization_chain(self, organization_id: UUID) -> VerificationResult:
        from odp_api.modules.audit.verify import verify_organization_chain

        return verify_organization_chain(organization_id, self.repository)

    def block_appends(self, organization_id: UUID) -> None:
        with self._health_lock:
            self._blocked_organizations.add(organization_id)

    def explicit_administrator_recovery(
        self, organization_id: UUID, actor: Actor
    ) -> VerificationResult:
        """Allow recovery only for an administrator after a fresh valid verification."""
        with self._health_lock:
            authorize(actor, "audit:recover", organization_id, None)
            result = self.verify_organization_chain(organization_id)
            if not result.is_valid:
                raise AuditRecoveryRejected(
                    f"Audit chain for organization {organization_id} remains invalid: {result.reason}."
                )
            self._blocked_organizations.discard(organization_id)
            return result
