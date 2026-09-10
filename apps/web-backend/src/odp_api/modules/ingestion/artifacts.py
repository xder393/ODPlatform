"""The explicit PostgreSQL/MinIO frame-artifact saga."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256 as sha256_digest
from typing import Any
from uuid import UUID

from odp_api.modules.tasks.admission import AdmissionPolicy
from odp_api.modules.tasks.models import TaskRecord
from odp_api.ports.evidence import EvidenceAuthorizationPort
from odp_api.ports.inspection_sessions import IngestionClaim
from odp_api.ports.storage import ObjectMetadata, ObjectStoragePort
from odp_api.ports.tasks import (
    AdmissionRejected,
    AdmissionRequest,
    CameraAdmissionPort,
)

__all__ = [
    "ArtifactHealth",
    "ArtifactSaga",
    "SelectedFrame",
]

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SelectedFrame:
    """A sampled frame ready for admission and immutable upload."""

    organization_id: UUID
    camera_id: UUID
    stream_session_id: UUID
    frame_sequence: int
    captured_at: datetime
    content: bytes
    correlation_id: UUID
    claim: IngestionClaim
    content_sha256: str | None = None
    content_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class ArtifactHealth:
    """Preflight state supplied by the frame ingestor before DB admission."""

    worker_healthy: bool
    redis_available: bool
    ready_count: int = 0
    oldest_ready_age_seconds: float = 0


class ArtifactSaga:
    """Coordinate camera admission, immutable upload, verification, and task creation.

    The service is synchronous because the P1A SQLAlchemy repository and the
    MinIO SDK are synchronous.  Process loops must call it via
    ``asyncio.to_thread`` rather than blocking an event loop.
    """

    def __init__(
        self,
        admission: CameraAdmissionPort,
        storage: ObjectStoragePort,
        *,
        policy: AdmissionPolicy | None = None,
        evidence_authorizer: EvidenceAuthorizationPort | None = None,
    ) -> None:
        self._admission = admission
        self._storage = storage
        self._policy = policy or AdmissionPolicy(frame_ttl_seconds=2)
        self._evidence_authorizer = evidence_authorizer

    def ingest(
        self,
        selected_frame: SelectedFrame,
        health: ArtifactHealth,
        now: datetime,
    ) -> TaskRecord | AdmissionRejected:
        """Admit and persist one already-selected frame.

        Health and frame-integrity rejection happens before ``reserve`` so a
        rejected frame cannot consume a camera reservation or create an
        object.  Expected saga failures are returned as typed
        :class:`AdmissionRejected` values so frame-ingestor loops can account
        for them without exception-driven control flow.
        """

        now = _utc(now)
        captured_at = _utc(selected_frame.captured_at)
        content = bytes(selected_frame.content)
        digest = sha256_digest(content).hexdigest()
        if selected_frame.content_sha256 is not None and (
            selected_frame.content_sha256.lower() != digest
        ):
            return AdmissionRejected("FRAME_HASH_MISMATCH")

        decision = self._policy.evaluate(
            ready_count=health.ready_count,
            oldest_ready_age_seconds=health.oldest_ready_age_seconds,
            worker_healthy=health.worker_healthy,
            redis_available=health.redis_available,
            frame_age_seconds=max(0.0, (now - captured_at).total_seconds()),
        )
        if not decision.accepted:
            return AdmissionRejected(decision.reason or "FRAME_REJECTED")

        request = AdmissionRequest(
            organization_id=selected_frame.organization_id,
            camera_id=selected_frame.camera_id,
            stream_session_id=selected_frame.stream_session_id,
            frame_sequence=selected_frame.frame_sequence,
            captured_at=captured_at,
            content_sha256=digest,
            correlation_id=selected_frame.correlation_id,
            claim=selected_frame.claim,
        )
        try:
            reservation = self._admission.reserve(request, now)
        except AdmissionRejected as error:
            return error

        object_key = _object_key(
            reservation.organization_id,
            reservation.artifact_id,
        )
        uploaded = False
        try:
            self._storage.put(
                object_key,
                content,
                sha256=digest,
                content_type=selected_frame.content_type,
            )
            uploaded = True
            metadata = self._storage.head(object_key)
            _verify_metadata(metadata, object_key, len(content), digest)
        except Exception as error:  # noqa: BLE001 - provider failures are mapped to saga outcomes
            if uploaded:
                LOGGER.warning(
                    "uploaded artifact failed verification; compensating object",
                    extra={"object_key": object_key, "error_type": type(error).__name__},
                )
            compensated = _best_effort_fail(
                self._admission,
                reservation,
                "STORAGE_UPLOAD_FAILED",
                now,
                selected_frame.claim,
            )
            if uploaded and compensated:
                _best_effort_delete(self._storage, object_key)
            return AdmissionRejected("STORAGE_UPLOAD_FAILED")

        try:
            return self._admission.complete_upload(
                reservation.reservation_id,
                reservation.organization_id,
                object_key,
                len(content),
                now,
                claim=selected_frame.claim,
            )
        except AdmissionRejected as error:
            # The object and PENDING row remain available to the reconciler.
            # In particular, do not call fail_upload after a second DB
            # transaction fails: that would erase the orphan candidate's
            # evidence before recovery can inspect it.
            return error
        except Exception as error:  # noqa: BLE001 - DB adapter failures become orphan candidates
            LOGGER.warning(
                "artifact upload committed but task promotion failed",
                extra={"object_key": object_key, "error_type": type(error).__name__},
            )
            return AdmissionRejected("TASK_COMMIT_FAILED")

    def presign_evidence(
        self,
        *,
        actor_id: UUID,
        organization_id: UUID,
        artifact_id: UUID,
        expires_seconds: int = 60,
    ) -> str:
        """Create a short-lived evidence URL after authorization.

        Resource authorization is delegated to an injected policy port.  A
        boolean supplied by an HTTP caller is intentionally not accepted: the
        policy must bind the actor, tenant, line scope, and artifact state.
        """

        if self._evidence_authorizer is None:
            raise AdmissionRejected("EVIDENCE_AUTHORIZER_REQUIRED")
        if expires_seconds < 1 or expires_seconds > 60:
            raise AdmissionRejected("INVALID_PRESIGN_TTL")
        try:
            self._evidence_authorizer.authorize(actor_id, organization_id, artifact_id)
        except AdmissionRejected:
            raise
        except Exception as error:
            raise AdmissionRejected("TENANT_SCOPE_REQUIRED") from error
        return self._storage.presign_get(
            _object_key(organization_id, artifact_id), expires_seconds
        )


def _object_key(organization_id: UUID, artifact_id: UUID) -> str:
    return f"organizations/{organization_id}/artifacts/{artifact_id}"


def _verify_metadata(
    metadata: ObjectMetadata | None,
    object_key: str,
    content_length: int,
    content_sha256: str,
) -> None:
    if metadata is None:
        raise ValueError("object HEAD returned no metadata")
    if metadata.object_key != object_key:
        raise ValueError("object HEAD key differs")
    if metadata.content_length != content_length:
        raise ValueError("object length differs")
    if metadata.sha256 is None or metadata.sha256.lower() != content_sha256:
        raise ValueError("object SHA-256 differs")


def _best_effort_fail(
    admission: CameraAdmissionPort,
    reservation: Any,
    error_code: str,
    now: datetime,
    claim: IngestionClaim,
) -> bool:
    try:
        admission.fail_upload(
            reservation.reservation_id,
            reservation.organization_id,
            error_code,
            now,
            claim=claim,
        )
        return True
    except Exception as error:  # noqa: BLE001 - never hide the primary storage failure
        LOGGER.warning(
            "artifact failure compensation did not complete",
            extra={"reservation_id": str(reservation.reservation_id), "error_type": type(error).__name__},
        )
        return False


def _best_effort_delete(storage: ObjectStoragePort, object_key: str) -> None:
    try:
        storage.delete(object_key)
    except Exception as error:  # noqa: BLE001 - cleanup must not mask the primary outcome
        LOGGER.warning(
            "artifact compensation delete did not complete",
            extra={"object_key": object_key, "error_type": type(error).__name__},
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
