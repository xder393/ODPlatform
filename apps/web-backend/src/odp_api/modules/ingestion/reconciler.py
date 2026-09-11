"""Bounded recovery and retention cleanup for frame artifacts.

The reconciler deliberately keeps object-storage observations outside the
database transaction.  The repository remains the authority for state
transitions and re-checks the reservation/reference predicates while writing
the result.  This prevents a stale HEAD or a cleanup list from reviving an
expired admission or deleting evidence that became referenced meanwhile.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from odp_api.ports.storage import ObjectMetadata, ObjectStoragePort

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingArtifact:
    """A bounded repository snapshot eligible for reconciliation/cleanup."""

    artifact_id: UUID
    organization_id: UUID
    object_key: str
    sha256: str
    content_length: int | None
    referenced: bool = False
    retention_until: datetime | None = None
    cleanup_reason: str | None = None
    cleanup_next_attempt_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    """Counters emitted by one bounded reconciliation pass."""

    promoted: int = 0
    failed: int = 0
    skipped_referenced: int = 0
    deleted: int = 0


class ArtifactReconciliationRepository(Protocol):
    """DB-side predicates and transitions for artifact recovery."""

    def pending_artifacts(self, now: datetime, limit: int) -> list[PendingArtifact]: ...

    def reconcile_pending_artifact(
        self,
        candidate: PendingArtifact,
        metadata: ObjectMetadata | None,
        now: datetime,
    ) -> bool: ...

    def cleanup_candidates(self, now: datetime, limit: int) -> list[PendingArtifact]: ...

    def mark_artifact_deleted(self, candidate: PendingArtifact, now: datetime) -> bool: ...

    def defer_artifact_cleanup(self, candidate: PendingArtifact, now: datetime) -> bool: ...


class ArtifactReconciler:
    """Repair orphaned uploads, then perform protected retention cleanup."""

    def __init__(
        self,
        repository: ArtifactReconciliationRepository,
        storage: ObjectStoragePort,
    ) -> None:
        self._repository = repository
        self._storage = storage

    def run_once(self, now: datetime, *, limit: int = 100) -> ReconcileSummary:
        """Run one bounded pass; individual provider failures do not abort it."""

        if limit < 1:
            raise ValueError("limit must be positive")

        promoted = failed = skipped_referenced = deleted = 0
        for candidate in self._repository.pending_artifacts(now, limit):
            metadata: ObjectMetadata | None
            try:
                metadata = self._storage.head(candidate.object_key)
            except Exception as error:  # noqa: BLE001 - continue bounded recovery
                LOGGER.warning(
                    "artifact HEAD failed during reconciliation",
                    extra={"artifact_id": str(candidate.artifact_id), "error_type": type(error).__name__},
                )
                metadata = None
            try:
                if self._repository.reconcile_pending_artifact(candidate, metadata, now):
                    promoted += 1
                else:
                    failed += 1
            except Exception as error:
                LOGGER.exception(
                    "artifact reconciliation transition failed",
                    extra={"artifact_id": str(candidate.artifact_id), "error_type": type(error).__name__},
                )
                failed += 1

        for candidate in self._repository.cleanup_candidates(now, limit):
            if candidate.referenced:
                skipped_referenced += 1
                continue
            if candidate.retention_until is not None and candidate.retention_until > now:
                continue
            try:
                self._storage.delete(candidate.object_key)
                if self._repository.mark_artifact_deleted(candidate, now):
                    deleted += 1
            except Exception as error:  # noqa: BLE001 - leave DB row for retry
                LOGGER.warning(
                    "artifact cleanup failed; candidate remains durable",
                    extra={"artifact_id": str(candidate.artifact_id), "error_type": type(error).__name__},
                )
                try:
                    self._repository.defer_artifact_cleanup(candidate, now)
                except Exception as defer_error:  # noqa: BLE001 - preserve the provider failure
                    LOGGER.warning(
                        "artifact cleanup retry pacing could not be persisted",
                        extra={
                            "artifact_id": str(candidate.artifact_id),
                            "error_type": type(defer_error).__name__,
                        },
                    )

        return ReconcileSummary(
            promoted=promoted,
            failed=failed,
            skipped_referenced=skipped_referenced,
            deleted=deleted,
        )


__all__ = [
    "ArtifactReconciler",
    "ArtifactReconciliationRepository",
    "PendingArtifact",
    "ReconcileSummary",
]
