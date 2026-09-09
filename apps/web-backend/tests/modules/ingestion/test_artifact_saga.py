"""Tests for the MinIO/PostgreSQL frame-artifact saga."""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from odp_api.modules.ingestion.artifacts import (
    ArtifactHealth,
    ArtifactSaga,
    SelectedFrame,
)
from odp_api.modules.tasks.models import TaskRecord, TaskStatus
from odp_api.ports.storage import ObjectMetadata
from odp_api.ports.tasks import AdmissionRejected, AdmissionReservation

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def frame(*, content: bytes = b"jpeg-bytes") -> SelectedFrame:
    return SelectedFrame(
        organization_id=UUID("00000000-0000-0000-0000-000000000001"),
        camera_id=UUID("00000000-0000-0000-0000-000000000002"),
        stream_session_id=UUID("00000000-0000-0000-0000-000000000003"),
        frame_sequence=1,
        captured_at=NOW,
        content=content,
        correlation_id=UUID("00000000-0000-0000-0000-000000000004"),
    )


def healthy_dependencies() -> ArtifactHealth:
    return ArtifactHealth(
        worker_healthy=True,
        redis_available=True,
        ready_count=0,
        oldest_ready_age_seconds=0,
    )


def unhealthy_workers() -> ArtifactHealth:
    return ArtifactHealth(
        worker_healthy=False,
        redis_available=True,
        ready_count=0,
        oldest_ready_age_seconds=0,
    )


class RecordingStorage:
    def __init__(self):
        self.calls: list[str] = []
        self.put_calls: list[tuple[str, bytes, str]] = []
        self.objects: dict[str, ObjectMetadata] = {}

    def put(self, object_key, content, *, sha256, content_type="application/octet-stream"):
        del content_type
        self.calls.append("put")
        self.put_calls.append((object_key, content, sha256))
        metadata = ObjectMetadata(object_key, len(content), sha256)
        self.objects[object_key] = metadata
        return metadata

    def head(self, object_key):
        self.calls.append("head")
        return self.objects.get(object_key)

    def get(self, object_key):
        self.calls.append("get")
        raise NotImplementedError

    def delete(self, object_key):
        self.calls.append("delete")
        self.objects.pop(object_key, None)

    def presign_get(self, object_key, expires_seconds=60):
        self.calls.append("presign_get")
        return f"https://storage.test/{object_key}?expires={expires_seconds}"


class RecordingAdmission:
    def __init__(self, *, complete_error: Exception | None = None):
        self.reservation = AdmissionReservation(
            reservation_id=uuid4(),
            artifact_id=uuid4(),
            organization_id=UUID("00000000-0000-0000-0000-000000000001"),
            camera_id=UUID("00000000-0000-0000-0000-000000000002"),
            stream_session_id=UUID("00000000-0000-0000-0000-000000000003"),
            frame_sequence=1,
            captured_at=NOW,
            content_sha256=sha256(b"jpeg-bytes").hexdigest(),
        )
        self.complete_error = complete_error
        self.calls: list[str] = []
        self.failed: list[str] = []

    def reserve(self, request, now):
        self.calls.append("reserve")
        assert request.content_sha256 == sha256(b"jpeg-bytes").hexdigest()
        return self.reservation

    def complete_upload(self, reservation_id, organization_id, object_key, content_length, now):
        self.calls.append("complete_upload")
        if self.complete_error:
            raise self.complete_error
        return TaskRecord(
            task_id=uuid4(),
            task_type="vision_inference",
            idempotency_key="idempotency",
            payload={"artifact_id": str(self.reservation.artifact_id)},
            status=TaskStatus.READY,
            attempt_count=0,
            created_at=NOW,
            organization_id=organization_id,
            camera_id=self.reservation.camera_id,
            artifact_id=self.reservation.artifact_id,
        )

    def fail_upload(self, reservation_id, organization_id, error_code, now):
        self.calls.append("fail_upload")
        self.failed.append(error_code)


class RecordingEvidenceAuthorizer:
    def __init__(self, *, allowed=True):
        self.allowed = allowed
        self.calls = []

    def authorize(self, actor_id, organization_id, artifact_id):
        self.calls.append((actor_id, organization_id, artifact_id))
        if not self.allowed:
            raise AdmissionRejected("TENANT_SCOPE_REQUIRED")


def test_rejected_frame_never_uploads():
    storage = RecordingStorage()
    admission = RecordingAdmission()
    saga = ArtifactSaga(admission, storage)

    result = saga.ingest(frame(), unhealthy_workers(), NOW)

    assert isinstance(result, AdmissionRejected)
    assert result.reason == "WORKER_UNHEALTHY"
    assert storage.put_calls == []
    assert admission.calls == []


def test_upload_is_verified_before_task_becomes_ready():
    storage = RecordingStorage()
    admission = RecordingAdmission()
    saga = ArtifactSaga(admission, storage)

    task = saga.ingest(frame(), healthy_dependencies(), NOW)

    assert isinstance(task, TaskRecord)
    assert storage.calls[:2] == ["put", "head"]
    assert admission.calls == ["reserve", "complete_upload"]
    assert task.status is TaskStatus.READY
    assert storage.put_calls[0][0] == (
        f"organizations/{frame().organization_id}/artifacts/{admission.reservation.artifact_id}"
    )


def test_hash_mismatch_is_rejected_before_reservation():
    storage = RecordingStorage()
    admission = RecordingAdmission()
    saga = ArtifactSaga(admission, storage)
    selected = replace(frame(), content_sha256="0" * 64)

    result = saga.ingest(selected, healthy_dependencies(), NOW)

    assert isinstance(result, AdmissionRejected)
    assert result.reason == "FRAME_HASH_MISMATCH"
    assert admission.calls == []
    assert storage.calls == []


def test_upload_failure_compensates_reservation():
    class FailingStorage(RecordingStorage):
        def put(self, *args, **kwargs):
            self.calls.append("put")
            raise OSError("storage unavailable")

    storage = FailingStorage()
    admission = RecordingAdmission()
    saga = ArtifactSaga(admission, storage)

    result = saga.ingest(frame(), healthy_dependencies(), NOW)

    assert isinstance(result, AdmissionRejected)
    assert result.reason == "STORAGE_UPLOAD_FAILED"
    assert admission.failed == ["STORAGE_UPLOAD_FAILED"]


def test_uploaded_object_is_deleted_when_post_upload_integrity_check_fails():
    class MismatchedStorage(RecordingStorage):
        def head(self, object_key):
            self.calls.append("head")
            return ObjectMetadata(object_key, 999, "f" * 64)

    storage = MismatchedStorage()
    admission = RecordingAdmission()
    saga = ArtifactSaga(admission, storage)

    result = saga.ingest(frame(), healthy_dependencies(), NOW)

    assert isinstance(result, AdmissionRejected)
    assert result.reason == "STORAGE_UPLOAD_FAILED"
    assert admission.failed == ["STORAGE_UPLOAD_FAILED"]
    assert storage.calls == ["put", "head", "delete"]


def test_database_completion_failure_leaves_uploaded_object_for_reconciliation():
    storage = RecordingStorage()
    admission = RecordingAdmission(complete_error=RuntimeError("database unavailable"))
    saga = ArtifactSaga(admission, storage)

    result = saga.ingest(frame(), healthy_dependencies(), NOW)

    assert isinstance(result, AdmissionRejected)
    assert result.reason == "TASK_COMMIT_FAILED"
    assert "complete_upload" in admission.calls
    assert admission.failed == []
    assert storage.objects


def test_presign_requires_authorized_tenant_and_returns_short_ttl():
    storage = RecordingStorage()
    admission = RecordingAdmission()
    denied = ArtifactSaga(
        admission,
        storage,
        evidence_authorizer=RecordingEvidenceAuthorizer(allowed=False),
    )
    artifact_id = admission.reservation.artifact_id

    with pytest.raises(AdmissionRejected, match="TENANT_SCOPE_REQUIRED"):
        denied.presign_evidence(actor_id=uuid4(), organization_id=uuid4(), artifact_id=artifact_id)

    authorizer = RecordingEvidenceAuthorizer()
    saga = ArtifactSaga(admission, storage, evidence_authorizer=authorizer)
    actor_id = uuid4()
    url = saga.presign_evidence(
        actor_id=actor_id,
        organization_id=admission.reservation.organization_id,
        artifact_id=artifact_id,
    )
    assert "expires=60" in url
    assert storage.calls[-1] == "presign_get"
    assert authorizer.calls == [(actor_id, admission.reservation.organization_id, artifact_id)]


def test_presign_fails_closed_without_resource_authorizer():
    saga = ArtifactSaga(RecordingAdmission(), RecordingStorage())
    with pytest.raises(AdmissionRejected, match="EVIDENCE_AUTHORIZER_REQUIRED"):
        saga.presign_evidence(actor_id=uuid4(), organization_id=uuid4(), artifact_id=uuid4())
