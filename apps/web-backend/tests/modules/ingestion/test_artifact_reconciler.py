"""Tests for bounded Artifact reconciliation and protected cleanup."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from odp_api.modules.ingestion.reconciler import ArtifactReconciler, PendingArtifact
from odp_api.ports.storage import ObjectMetadata

NOW = datetime(2026, 9, 7, tzinfo=UTC)


@dataclass
class FakeRepository:
    pending: list[PendingArtifact]
    cleanup: list[PendingArtifact]

    def __post_init__(self):
        self.promoted = []
        self.failed = []
        self.deleted = []

    def pending_artifacts(self, now, limit):
        return self.pending[:limit]

    def reconcile_pending_artifact(self, candidate, metadata, now):
        if metadata is None:
            self.failed.append((candidate.artifact_id, "OBJECT_MISSING"))
            return False
        if metadata.content_length != candidate.content_length or metadata.sha256 != candidate.sha256:
            self.failed.append((candidate.artifact_id, "OBJECT_INTEGRITY_MISMATCH"))
            return False
        self.promoted.append(candidate.artifact_id)
        return True

    def cleanup_candidates(self, now, limit):
        return self.cleanup[:limit]

    def mark_artifact_deleted(self, candidate, now):
        self.deleted.append(candidate.artifact_id)
        return True


class FakeStorage:
    def __init__(self, objects):
        self.objects = objects
        self.deleted = []

    def head(self, key):
        return self.objects.get(key)

    def delete(self, key):
        self.deleted.append(key)


def candidate(*, referenced=False):
    artifact_id = uuid4()
    return PendingArtifact(
        artifact_id=artifact_id,
        organization_id=uuid4(),
        object_key=f"organizations/test/artifacts/{artifact_id}",
        sha256="a" * 64,
        content_length=4,
        referenced=referenced,
        retention_until=NOW - timedelta(seconds=1),
    )


def test_reconciler_promotes_only_matching_object_metadata():
    item = candidate()
    repository = FakeRepository([item], [])
    storage = FakeStorage(
        {
            item.object_key: ObjectMetadata(item.object_key, 4, "a" * 64),
        }
    )

    result = ArtifactReconciler(repository, storage).run_once(NOW)

    assert result.promoted == 1
    assert repository.promoted == [item.artifact_id]
    assert repository.failed == []


def test_reconciler_fails_missing_object_without_reviving_reservation():
    item = candidate()
    repository = FakeRepository([item], [])

    result = ArtifactReconciler(repository, FakeStorage({})).run_once(NOW)

    assert result.failed == 1
    assert repository.promoted == []
    assert repository.failed == [(item.artifact_id, "OBJECT_MISSING")]


def test_cleaner_skips_referenced_evidence_and_marks_deleted_after_storage_delete():
    protected = candidate(referenced=True)
    deletable = candidate()
    repository = FakeRepository([], [protected, deletable])
    storage = FakeStorage({deletable.object_key: ObjectMetadata(deletable.object_key, 4, "a" * 64)})

    result = ArtifactReconciler(repository, storage).run_once(NOW)

    assert result.skipped_referenced == 1
    assert result.deleted == 1
    assert storage.deleted == [deletable.object_key]
    assert repository.deleted == [deletable.artifact_id]
