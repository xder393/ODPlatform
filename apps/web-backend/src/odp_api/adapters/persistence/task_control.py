"""PostgreSQL-owned camera admission and upload-completion transactions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from odp_api.adapters.persistence.models import InspectionEventRow
from odp_api.adapters.persistence.repositories import SqlAlchemyAuditSessionRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
    InspectionSessionRow,
    MessageQuarantineRow,
    OutboxEventRow,
)
from odp_api.modules.audit.models import AuditCommand, audit_log_from_command
from odp_api.modules.ingestion.reconciler import PendingArtifact
from odp_api.modules.tasks.commands import (
    DeliveryDecision,
    DeliveryOutcome,
    DeliveryRequest,
    LeaseClaim,
    QuarantineReason,
    UnscopedQuarantineCommand,
    WorkerDeliveryScope,
)
from odp_api.modules.tasks.models import (
    ArtifactLifecycle,
    ArtifactState,
    FailureKind,
    TaskRecord,
    TaskStatus,
)
from odp_api.modules.tasks.recovery import (
    QuarantineResult,
    ReplayResult,
    SystemRecoveryScope,
)
from odp_api.ports.storage import ObjectMetadata
from odp_api.ports.tasks import (
    AdmissionRejected,
    AdmissionRequest,
    AdmissionReservation,
    CameraAdmissionPort,
    StaleLease,
    TaskExecutionPort,
)

RESERVATION_TTL_SECONDS = 30
LEASE_SECONDS = 20
MAX_ATTEMPTS = 3
RENEW_INTERVAL_SECONDS = 5
INFERENCE_TIMEOUT_SECONDS = 10
TASK_TYPE = "vision_inference"
INFERENCE_REQUEST_EVENT = "vision.inference.requested.v1"
EVENT_SCHEMA_VERSION = 1
REDISPATCH_AFTER_SECONDS = 10
MAX_QUARANTINE_PAYLOAD_BYTES = 65536
RECOVERY_BATCH_SIZE = 100


class SqlAlchemyTaskControlRepository(CameraAdmissionPort, TaskExecutionPort):
    """Persist admission facts with one serialized camera-state lock.

    Lock order for all admission operations is deliberately explicit:
    ``camera_inference_state`` first, then the Artifact, then any Task/Outbox
    rows.  Task 3's claim, renewal, and finalize operations must follow this
    same order before acquiring task or attempt locks.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[Session], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def reserve(self, request: AdmissionRequest, now: datetime) -> AdmissionReservation:
        """Reserve one upload slot and persist a PENDING Artifact atomically."""

        _validate_request(request)
        with self._session_factory() as session:
            try:
                current_time = self._db_now(session)
                state = self._lock_camera_state(session, request.organization_id, request.camera_id)
                self._require_session(session, request)

                existing = session.scalar(
                    select(FrameArtifactRow)
                    .where(
                        FrameArtifactRow.organization_id == request.organization_id,
                        FrameArtifactRow.camera_id == request.camera_id,
                        FrameArtifactRow.stream_session_id == request.stream_session_id,
                        FrameArtifactRow.frame_sequence == request.frame_sequence,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if (
                        existing.sha256 == request.content_sha256
                        and existing.state == ArtifactState.PENDING.value
                        and state.reservation_id == existing.artifact_id
                    ):
                        session.commit()
                        return _reservation_from_artifact(existing)
                    raise AdmissionRejected("FRAME_ALREADY_ADMITTED")

                if state.reservation_id is not None:
                    if (
                        state.reservation_expires_at is not None
                        and _as_utc(state.reservation_expires_at) <= current_time
                    ):
                        self._expire_reservation(session, state, current_time)
                    else:
                        raise AdmissionRejected("ADMISSION_IN_PROGRESS")

                ready_count = self._sync_ready_count(session, state)
                evicted_task_id: UUID | None = None
                while ready_count >= 2:
                    evicted_task_id = self._evict_oldest_ready(
                        session,
                        request.organization_id,
                        request.camera_id,
                        current_time,
                    )
                    if evicted_task_id is None:
                        raise AdmissionRejected("READY_WINDOW_FULL")
                    ready_count = self._sync_ready_count(session, state)

                artifact_id = uuid4()
                artifact = FrameArtifactRow(
                    artifact_id=artifact_id,
                    organization_id=request.organization_id,
                    camera_id=request.camera_id,
                    stream_session_id=request.stream_session_id,
                    frame_sequence=request.frame_sequence,
                    captured_at=_as_utc(request.captured_at),
                    object_key=None,
                    sha256=request.content_sha256,
                    content_length=None,
                    state=ArtifactState.PENDING.value,
                    lifecycle=ArtifactLifecycle.PROCESSING.value,
                    retention_until=None,
                    created_at=current_time,
                    updated_at=current_time,
                )
                session.add(artifact)
                state.reservation_id = artifact_id
                state.reservation_expires_at = current_time + timedelta(
                    seconds=RESERVATION_TTL_SECONDS
                )
                state.version += 1
                state.updated_at = current_time
                session.commit()
                return AdmissionReservation(
                    reservation_id=artifact_id,
                    artifact_id=artifact_id,
                    organization_id=request.organization_id,
                    camera_id=request.camera_id,
                    stream_session_id=request.stream_session_id,
                    frame_sequence=request.frame_sequence,
                    captured_at=_as_utc(request.captured_at),
                    content_sha256=request.content_sha256,
                    evicted_task_id=evicted_task_id,
                )
            except BaseException:
                session.rollback()
                raise

    def complete_upload(
        self,
        reservation_id: UUID,
        organization_id: UUID,
        object_key: str,
        content_length: int,
        now: datetime,
    ) -> TaskRecord:
        """Promote an uploaded Artifact and create its READY Task and Outbox."""

        if not object_key.strip():
            raise AdmissionRejected("INVALID_OBJECT_KEY")
        if content_length < 0:
            raise AdmissionRejected("INVALID_CONTENT_LENGTH")
        with self._session_factory() as session:
            try:
                current_time = self._db_now(session)
                # The reservation capability identifies the camera row. Lock it
                # before reading/updating the Artifact to preserve the shared
                # admission -> claim -> finalize lock order.
                state = self._lock_camera_state_for_reservation(
                    session, organization_id, reservation_id
                )
                if state is None or state.reservation_id != reservation_id:
                    raise AdmissionRejected("RESERVATION_NOT_FOUND")
                artifact = session.scalar(
                    select(FrameArtifactRow)
                    .where(
                        FrameArtifactRow.artifact_id == reservation_id,
                        FrameArtifactRow.organization_id == state.organization_id,
                        FrameArtifactRow.camera_id == state.camera_id,
                    )
                    .with_for_update()
                )
                if artifact is None:
                    raise AdmissionRejected("RESERVATION_NOT_FOUND")
                if artifact.state != ArtifactState.PENDING.value:
                    raise AdmissionRejected("ARTIFACT_NOT_PENDING")

                result = self._promote_pending_locked(
                    session, state, artifact, object_key, content_length, current_time
                )
                session.commit()
                return result
            except BaseException:
                session.rollback()
                raise

    def fail_upload(
        self, reservation_id: UUID, organization_id: UUID, error_code: str, now: datetime
    ) -> None:
        """Mark a pending Artifact failed and release its camera reservation."""

        if not error_code.strip():
            raise AdmissionRejected("INVALID_ERROR_CODE")
        with self._session_factory() as session:
            try:
                current_time = self._db_now(session)
                state = self._lock_camera_state_for_reservation(
                    session, organization_id, reservation_id
                )
                if state is None or state.reservation_id != reservation_id:
                    raise AdmissionRejected("RESERVATION_NOT_FOUND")
                artifact = session.scalar(
                    select(FrameArtifactRow)
                    .where(
                        FrameArtifactRow.artifact_id == reservation_id,
                        FrameArtifactRow.organization_id == state.organization_id,
                        FrameArtifactRow.camera_id == state.camera_id,
                    )
                    .with_for_update()
                )
                if artifact is None:
                    raise AdmissionRejected("RESERVATION_NOT_FOUND")
                if artifact.state == ArtifactState.PENDING.value:
                    artifact.state = ArtifactState.FAILED.value
                    artifact.error_code = error_code
                    artifact.error_detail = error_code
                    artifact.updated_at = current_time
                elif artifact.state != ArtifactState.FAILED.value:
                    raise AdmissionRejected("ARTIFACT_NOT_PENDING")
                state.reservation_id = None
                state.reservation_expires_at = None
                state.version += 1
                state.updated_at = current_time
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def pending_artifacts(self, now: datetime, limit: int) -> list[PendingArtifact]:
        """List orphan candidates without granting them promotion authority."""

        self._require_positive_recovery_limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(
                select(FrameArtifactRow)
                .join(
                    CameraInferenceStateRow,
                    (CameraInferenceStateRow.organization_id == FrameArtifactRow.organization_id)
                    & (CameraInferenceStateRow.camera_id == FrameArtifactRow.camera_id)
                    & (CameraInferenceStateRow.reservation_id == FrameArtifactRow.artifact_id),
                )
                .where(
                    FrameArtifactRow.state == ArtifactState.PENDING.value,
                    FrameArtifactRow.updated_at <= now,
                )
                .order_by(FrameArtifactRow.updated_at, FrameArtifactRow.artifact_id)
                .limit(limit)
            ).all()
            return [_pending_artifact(row) for row in rows]

    def reconcile_pending_artifact(
        self,
        candidate: PendingArtifact,
        metadata: ObjectMetadata | None,
        now: datetime,
    ) -> bool:
        """Promote only a still-live reservation with matching object metadata."""

        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                state = self._lock_camera_state_for_reservation(
                    session, candidate.organization_id, candidate.artifact_id
                )
                if state is None:
                    session.commit()
                    return False
                if state.reservation_expires_at is None or _as_utc(state.reservation_expires_at) <= current:
                    self._expire_reservation(session, state, current)
                    session.commit()
                    return False
                artifact = session.scalar(
                    select(FrameArtifactRow)
                    .where(
                        FrameArtifactRow.artifact_id == candidate.artifact_id,
                        FrameArtifactRow.organization_id == state.organization_id,
                        FrameArtifactRow.camera_id == state.camera_id,
                    )
                    .with_for_update()
                )
                if artifact is None or artifact.state != ArtifactState.PENDING.value:
                    session.commit()
                    return False
                if not _metadata_matches(candidate, metadata):
                    self._fail_pending_locked(
                        session, state, artifact, "OBJECT_MISSING" if metadata is None else "OBJECT_INTEGRITY_MISMATCH", current
                    )
                    session.commit()
                    return False
                self._promote_pending_locked(
                    session,
                    state,
                    artifact,
                    candidate.object_key,
                    metadata.content_length,
                    current,
                )
                session.commit()
                return True
            except BaseException:
                session.rollback()
                raise

    def cleanup_candidates(self, now: datetime, limit: int) -> list[PendingArtifact]:
        """Return expired AVAILABLE artifacts, annotated with evidence references."""

        self._require_positive_recovery_limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(
                select(FrameArtifactRow)
                .where(
                    FrameArtifactRow.state == ArtifactState.AVAILABLE.value,
                    FrameArtifactRow.lifecycle == ArtifactLifecycle.EVIDENCE.value,
                    FrameArtifactRow.retention_until.is_not(None),
                    FrameArtifactRow.retention_until <= now,
                    FrameArtifactRow.object_key.is_not(None),
                )
                .order_by(FrameArtifactRow.retention_until, FrameArtifactRow.artifact_id)
                .limit(limit)
            ).all()
            result: list[PendingArtifact] = []
            for row in rows:
                referenced = session.scalar(
                    select(InspectionEventRow.event_id)
                    .where(
                        InspectionEventRow.organization_id == row.organization_id,
                        InspectionEventRow.evidence_artifact_id == row.artifact_id,
                    )
                    .limit(1)
                ) is not None
                result.append(_pending_artifact(row, referenced=referenced))
            return result

    def mark_artifact_deleted(self, candidate: PendingArtifact, now: datetime) -> bool:
        """Mark a retention candidate deleted after locking and rechecking references."""

        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                snapshot = session.scalar(
                    select(FrameArtifactRow).where(
                        FrameArtifactRow.artifact_id == candidate.artifact_id,
                        FrameArtifactRow.organization_id == candidate.organization_id,
                    )
                )
                if snapshot is None:
                    session.commit()
                    return False
                state = session.scalar(
                    select(CameraInferenceStateRow)
                    .where(
                        CameraInferenceStateRow.organization_id == snapshot.organization_id,
                        CameraInferenceStateRow.camera_id == snapshot.camera_id,
                    )
                    .with_for_update()
                )
                if state is None:
                    session.commit()
                    return False
                artifact = session.scalar(
                    select(FrameArtifactRow)
                    .where(
                        FrameArtifactRow.artifact_id == candidate.artifact_id,
                        FrameArtifactRow.organization_id == state.organization_id,
                        FrameArtifactRow.camera_id == state.camera_id,
                    )
                    .with_for_update()
                )
                if artifact is None or artifact.state != ArtifactState.AVAILABLE.value:
                    session.commit()
                    return False
                if artifact.retention_until is None or _as_utc(artifact.retention_until) > current:
                    session.commit()
                    return False
                if session.scalar(
                    select(InspectionEventRow.event_id)
                    .where(
                        InspectionEventRow.organization_id == artifact.organization_id,
                        InspectionEventRow.evidence_artifact_id == artifact.artifact_id,
                    )
                    .limit(1)
                ) is not None:
                    session.commit()
                    return False
                live_task = session.scalar(
                    select(InferenceTaskRow.task_id)
                    .where(
                        InferenceTaskRow.organization_id == artifact.organization_id,
                        InferenceTaskRow.camera_id == artifact.camera_id,
                        InferenceTaskRow.artifact_id == artifact.artifact_id,
                        InferenceTaskRow.status.in_(
                            [
                                TaskStatus.READY.value,
                                TaskStatus.RUNNING.value,
                                TaskStatus.RETRY_WAIT.value,
                            ]
                        ),
                    )
                    .limit(1)
                )
                if live_task is not None:
                    session.commit()
                    return False
                artifact.state = ArtifactState.DELETED.value
                artifact.updated_at = current
                session.commit()
                return True
            except BaseException:
                session.rollback()
                raise

    @staticmethod
    def _fail_pending_locked(
        session: Session,
        state: CameraInferenceStateRow,
        artifact: FrameArtifactRow,
        error_code: str,
        now: datetime,
    ) -> None:
        artifact.state = ArtifactState.FAILED.value
        artifact.error_code = error_code
        artifact.error_detail = error_code
        artifact.updated_at = now
        state.reservation_id = None
        state.reservation_expires_at = None
        state.version += 1
        state.updated_at = now

    @staticmethod
    def _promote_pending_locked(
        session: Session,
        state: CameraInferenceStateRow,
        artifact: FrameArtifactRow,
        object_key: str,
        content_length: int,
        now: datetime,
    ) -> TaskRecord:
        artifact.state = ArtifactState.AVAILABLE.value
        artifact.object_key = object_key
        artifact.content_length = content_length
        artifact.updated_at = now
        task = session.scalar(
            select(InferenceTaskRow).where(
                InferenceTaskRow.organization_id == state.organization_id,
                InferenceTaskRow.camera_id == state.camera_id,
                InferenceTaskRow.artifact_id == artifact.artifact_id,
            )
        )
        if task is None:
            task = InferenceTaskRow(
                task_id=uuid4(),
                organization_id=state.organization_id,
                camera_id=state.camera_id,
                artifact_id=artifact.artifact_id,
                idempotency_key=_task_idempotency_key(artifact),
                status=TaskStatus.READY.value,
                dispatch_seq=1,
                attempt_count=0,
                next_attempt_at=None,
                last_dispatched_at=None,
                lease_owner=None,
                fence_token=0,
                lease_expires_at=None,
                error_code=None,
                error_detail=None,
                created_at=now,
                updated_at=now,
            )
            session.add(task)
            session.flush()
        elif task.status != TaskStatus.READY.value:
            raise AdmissionRejected("TASK_ALREADY_FINALIZED")
        outbox = session.scalar(
            select(OutboxEventRow)
            .where(
                OutboxEventRow.organization_id == state.organization_id,
                OutboxEventRow.task_id == task.task_id,
                OutboxEventRow.dispatch_seq == 1,
                OutboxEventRow.event_type == INFERENCE_REQUEST_EVENT,
            )
            .with_for_update()
        )
        if outbox is None:
            session.add(
                OutboxEventRow(
                    outbox_id=uuid4(),
                    organization_id=state.organization_id,
                    aggregate_type="inference_task",
                    aggregate_id=task.task_id,
                    task_id=task.task_id,
                    dispatch_seq=1,
                    event_type=INFERENCE_REQUEST_EVENT,
                    schema_version=EVENT_SCHEMA_VERSION,
                    payload={"task_id": str(task.task_id), "dispatch_seq": 1},
                    available_at=now,
                    claim_owner=None,
                    claim_expires_at=None,
                    publish_attempts=0,
                    published_at=None,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        state.reservation_id = None
        state.reservation_expires_at = None
        state.last_admitted_at = now
        state.version += 1
        state.updated_at = now
        state.ready_count = SqlAlchemyTaskControlRepository._sync_ready_count(session, state)
        if state.ready_count > 2:
            raise AdmissionRejected("READY_WINDOW_FULL")
        return _task_record(task)

    def get_task(self, task_id: UUID, organization_id: UUID) -> TaskRecord | None:
        """Read a task only inside its tenant scope."""

        with self._session_factory() as session:
            row = session.scalar(
                select(InferenceTaskRow).where(
                    InferenceTaskRow.task_id == task_id,
                    InferenceTaskRow.organization_id == organization_id,
                )
            )
            return _task_record(row) if row is not None else None

    def accept_delivery(
        self,
        request: DeliveryRequest,
        worker_id: str,
        now: datetime,
        scope: WorkerDeliveryScope,
    ) -> DeliveryDecision:
        """Atomically compare dispatch authority and claim or quarantine delivery."""

        self._require_worker_delivery_scope(scope, worker_id)
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                existing_quarantine = session.scalar(
                    select(MessageQuarantineRow)
                    .where(
                        MessageQuarantineRow.stream_name
                        == _bounded_text(request.stream_name, 255),
                        MessageQuarantineRow.message_id
                        == _bounded_text(request.message_id, 255),
                    )
                    .with_for_update()
                )
                if existing_quarantine is not None:
                    # A durable quarantine is the authority for this Redis
                    # identity.  Do not reinterpret it when task metadata or
                    # dispatch state changes after a failed XACK.
                    session.commit()
                    return DeliveryDecision(DeliveryOutcome.QUARANTINED)
                candidate = session.scalar(
                    select(InferenceTaskRow).where(
                        InferenceTaskRow.task_id == request.task_id,
                        InferenceTaskRow.organization_id == request.organization_id,
                    )
                )
                if candidate is None:
                    self._append_quarantine(
                        session,
                        stream_name=request.stream_name,
                        message_id=request.message_id,
                        event_id=request.event_id,
                        event_type=request.event_type,
                        schema_version=request.schema_version,
                        raw_payload=request.raw_payload,
                        reason=QuarantineReason.UNRESOLVED_TASK_REFERENCE,
                        organization_id=request.organization_id,
                        task_id=None,
                        now=current,
                    )
                    session.commit()
                    return DeliveryDecision(DeliveryOutcome.QUARANTINED)

                state = self._lock_camera_state(
                    session, request.organization_id, candidate.camera_id
                )
                task = session.scalar(
                    select(InferenceTaskRow)
                    .where(
                        InferenceTaskRow.task_id == request.task_id,
                        InferenceTaskRow.organization_id == request.organization_id,
                    )
                    .with_for_update()
                )
                if task is None:
                    self._append_quarantine(
                        session,
                        stream_name=request.stream_name,
                        message_id=request.message_id,
                        event_id=request.event_id,
                        event_type=request.event_type,
                        schema_version=request.schema_version,
                        raw_payload=request.raw_payload,
                        reason=QuarantineReason.UNRESOLVED_TASK_REFERENCE,
                        organization_id=request.organization_id,
                        task_id=None,
                        now=current,
                    )
                    session.commit()
                    return DeliveryDecision(DeliveryOutcome.QUARANTINED)

                if request.expected_dispatch_seq < task.dispatch_seq:
                    session.commit()
                    return DeliveryDecision(DeliveryOutcome.DUPLICATE)
                if request.expected_dispatch_seq > task.dispatch_seq:
                    self._append_quarantine(
                        session,
                        stream_name=request.stream_name,
                        message_id=request.message_id,
                        event_id=request.event_id,
                        event_type=request.event_type,
                        schema_version=request.schema_version,
                        raw_payload=request.raw_payload,
                        reason=QuarantineReason.FUTURE_DISPATCH_SEQUENCE,
                        organization_id=request.organization_id,
                        # A future reference is an integrity failure, not a
                        # task-scoped compatibility decision.  Keep the
                        # tenant as an observation, but never attach the
                        # untrusted generation to a task row.
                        task_id=None,
                        now=current,
                    )
                    session.commit()
                    return DeliveryDecision(DeliveryOutcome.QUARANTINED)

                if _is_terminal_status(task.status):
                    session.commit()
                    return DeliveryDecision(DeliveryOutcome.DUPLICATE)

                if request.quarantine_reason is not None:
                    if task.status != TaskStatus.READY.value:
                        session.commit()
                        return DeliveryDecision(DeliveryOutcome.PENDING)
                    self._append_quarantine(
                        session,
                        stream_name=request.stream_name,
                        message_id=request.message_id,
                        event_id=request.event_id,
                        event_type=request.event_type,
                        schema_version=request.schema_version,
                        raw_payload=request.raw_payload,
                        reason=request.quarantine_reason,
                        organization_id=request.organization_id,
                        task_id=task.task_id,
                        now=current,
                    )
                    task.status = TaskStatus.BLOCKED_COMPATIBILITY.value
                    task.error_code = request.quarantine_reason.value
                    task.error_detail = "message quarantined"
                    task.updated_at = current
                    self._sync_ready_count(session, state)
                    state.version += 1
                    state.updated_at = current
                    session.commit()
                    return DeliveryDecision(DeliveryOutcome.QUARANTINED)

                claim = self._claim_locked(
                    session,
                    task=task,
                    state=state,
                    organization_id=request.organization_id,
                    worker_id=worker_id,
                    current=current,
                )
                session.commit()
                if claim is None:
                    return DeliveryDecision(DeliveryOutcome.PENDING)
                return DeliveryDecision(DeliveryOutcome.CLAIMED, claim)
            except BaseException:
                session.rollback()
                raise

    def quarantine_unscoped(
        self,
        command: UnscopedQuarantineCommand,
        now: datetime,
        scope: WorkerDeliveryScope,
    ) -> QuarantineResult:
        """Durably consume poison bytes without granting cross-tenant task mutation."""

        if not isinstance(scope, WorkerDeliveryScope):
            raise TypeError("unscoped quarantine requires a WorkerDeliveryScope")
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                row = self._append_quarantine(
                    session,
                    stream_name=command.stream_name,
                    message_id=command.message_id,
                    event_id=command.event_id,
                    event_type=command.event_type,
                    schema_version=command.schema_version,
                    raw_payload=command.raw_payload,
                    reason=command.reason,
                    organization_id=command.organization_id,
                    task_id=None,
                    now=current,
                )
                session.commit()
                return QuarantineResult(row.quarantine_id, ack_after_commit=True)
            except BaseException:
                session.rollback()
                raise

    def claim(
        self, task_id: UUID, organization_id: UUID, worker_id: str, now: datetime
    ) -> LeaseClaim | None:
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                task = session.scalar(
                    select(InferenceTaskRow).where(
                        InferenceTaskRow.task_id == task_id,
                        InferenceTaskRow.organization_id == organization_id,
                    )
                )
                if task is None:
                    session.commit()
                    return None
                # The camera anchor is the serialization point shared with admission
                # and finalize.  Do not lock the task before it.
                state = self._lock_camera_state(session, organization_id, task.camera_id)
                task = session.scalar(
                    select(InferenceTaskRow)
                    .where(
                        InferenceTaskRow.task_id == task_id,
                        InferenceTaskRow.organization_id == organization_id,
                    )
                    .with_for_update()
                )
                if task is None:
                    session.commit()
                    return None
                self._sync_ready_count(session, state)
                live = state.running_task_id is not None
                if live:
                    running = session.scalar(
                        select(InferenceTaskRow)
                        .where(
                            InferenceTaskRow.task_id == state.running_task_id,
                            InferenceTaskRow.organization_id == organization_id,
                        )
                        .with_for_update()
                    )
                    expired = running is not None and (
                        running.lease_expires_at is None
                        or _as_utc(running.lease_expires_at) <= current
                    )
                    if expired:
                        old_attempt = session.scalar(
                            select(InferenceAttemptRow)
                            .where(
                                InferenceAttemptRow.task_id == running.task_id,
                                InferenceAttemptRow.organization_id == organization_id,
                                InferenceAttemptRow.attempt_no == running.attempt_count,
                                InferenceAttemptRow.fence_token == running.fence_token,
                            )
                            .with_for_update()
                        )
                        if old_attempt is not None and old_attempt.finished_at is None:
                            old_attempt.finished_at = current
                            old_attempt.outcome = "LEASE_EXPIRED"
                        if running.task_id != task_id:
                            running.status = (
                                TaskStatus.DEAD_LETTER.value
                                if running.attempt_count >= MAX_ATTEMPTS
                                else TaskStatus.RETRY_WAIT.value
                            )
                            running.next_attempt_at = (
                                None if running.status == TaskStatus.DEAD_LETTER.value else current
                            )
                            running.lease_owner = None
                            running.lease_expires_at = None
                            running.updated_at = current
                            state.running_task_id = None
                        elif running.attempt_count >= MAX_ATTEMPTS:
                            running.status = TaskStatus.DEAD_LETTER.value
                            running.lease_owner = None
                            running.lease_expires_at = None
                            running.updated_at = current
                            state.running_task_id = None
                        else:
                            running.status = TaskStatus.READY.value
                            running.next_attempt_at = None
                            running.lease_owner = None
                            running.lease_expires_at = None
                            running.updated_at = current
                            state.running_task_id = None
                        state.version += 1
                        state.updated_at = current
                        live = False
                if (
                    live
                    and state.running_task_id == task_id
                    and task.lease_expires_at
                    and _as_utc(task.lease_expires_at) > current
                ):
                    session.commit()
                    return None
                if live or task.status != TaskStatus.READY.value:
                    session.commit()
                    return None
                task.status = TaskStatus.RUNNING.value
                task.attempt_count += 1
                task.fence_token += 1
                task.lease_owner = worker_id
                task.lease_expires_at = current + timedelta(seconds=LEASE_SECONDS)
                task.updated_at = current
                attempt = InferenceAttemptRow(
                    attempt_id=uuid4(),
                    task_id=task.task_id,
                    organization_id=organization_id,
                    worker_id=worker_id,
                    attempt_no=task.attempt_count,
                    fence_token=task.fence_token,
                    started_at=current,
                    finished_at=None,
                    outcome=None,
                    error_code=None,
                    error_detail=None,
                    duration_ms=None,
                )
                session.add(attempt)
                state.running_task_id = task_id
                self._sync_ready_count(session, state)
                state.version += 1
                state.updated_at = current
                session.commit()
                return LeaseClaim(
                    task_id,
                    organization_id,
                    task.artifact_id,
                    attempt.attempt_id,
                    task.attempt_count,
                    task.fence_token,
                    worker_id,
                    _as_utc(task.lease_expires_at),
                )
            except BaseException:
                session.rollback()
                raise

    def _claim_locked(
        self,
        session: Session,
        *,
        task: InferenceTaskRow,
        state: CameraInferenceStateRow,
        organization_id: UUID,
        worker_id: str,
        current: datetime,
    ) -> LeaseClaim | None:
        """Claim an already camera/task-locked row for the Worker delivery API."""

        self._sync_ready_count(session, state)
        live = state.running_task_id is not None
        if live:
            running = session.scalar(
                select(InferenceTaskRow)
                .where(
                    InferenceTaskRow.task_id == state.running_task_id,
                    InferenceTaskRow.organization_id == organization_id,
                )
                .with_for_update()
            )
            expired = running is not None and (
                running.lease_expires_at is None
                or _as_utc(running.lease_expires_at) <= current
            )
            if expired:
                old_attempt = session.scalar(
                    select(InferenceAttemptRow)
                    .where(
                        InferenceAttemptRow.task_id == running.task_id,
                        InferenceAttemptRow.organization_id == organization_id,
                        InferenceAttemptRow.attempt_no == running.attempt_count,
                        InferenceAttemptRow.fence_token == running.fence_token,
                    )
                    .with_for_update()
                )
                if old_attempt is not None and old_attempt.finished_at is None:
                    old_attempt.finished_at = current
                    old_attempt.outcome = "LEASE_EXPIRED"
                if running.task_id != task.task_id:
                    running.status = (
                        TaskStatus.DEAD_LETTER.value
                        if running.attempt_count >= MAX_ATTEMPTS
                        else TaskStatus.RETRY_WAIT.value
                    )
                    running.next_attempt_at = (
                        None if running.status == TaskStatus.DEAD_LETTER.value else current
                    )
                elif running.attempt_count >= MAX_ATTEMPTS:
                    running.status = TaskStatus.DEAD_LETTER.value
                else:
                    running.status = TaskStatus.READY.value
                    running.next_attempt_at = None
                running.lease_owner = None
                running.lease_expires_at = None
                running.updated_at = current
                state.running_task_id = None
                state.version += 1
                state.updated_at = current
                live = False
        if (
            live
            and state.running_task_id == task.task_id
            and task.lease_expires_at
            and _as_utc(task.lease_expires_at) > current
        ):
            return None
        if live or task.status != TaskStatus.READY.value:
            return None
        task.status = TaskStatus.RUNNING.value
        task.attempt_count += 1
        task.fence_token += 1
        task.lease_owner = worker_id
        task.lease_expires_at = current + timedelta(seconds=LEASE_SECONDS)
        task.updated_at = current
        attempt = InferenceAttemptRow(
            attempt_id=uuid4(),
            task_id=task.task_id,
            organization_id=organization_id,
            worker_id=worker_id,
            attempt_no=task.attempt_count,
            fence_token=task.fence_token,
            started_at=current,
            finished_at=None,
            outcome=None,
            error_code=None,
            error_detail=None,
            duration_ms=None,
        )
        session.add(attempt)
        state.running_task_id = task.task_id
        self._sync_ready_count(session, state)
        state.version += 1
        state.updated_at = current
        return LeaseClaim(
            task.task_id,
            organization_id,
            task.artifact_id,
            attempt.attempt_id,
            task.attempt_count,
            task.fence_token,
            worker_id,
            _as_utc(task.lease_expires_at),
        )

    def _append_quarantine(
        self,
        session: Session,
        *,
        stream_name: str,
        message_id: str,
        event_id: UUID,
        event_type: str,
        schema_version: str,
        raw_payload: bytes,
        reason: QuarantineReason,
        organization_id: UUID | None,
        task_id: UUID | None,
        now: datetime,
    ) -> MessageQuarantineRow:
        values = {
            "quarantine_id": uuid4(),
            "stream_name": _bounded_text(stream_name, 255),
            "message_id": _bounded_text(message_id, 255),
            "event_id": event_id,
            "event_type": _bounded_text(event_type, 255),
            "schema_version": _bounded_text(schema_version, 32),
            "raw_payload": raw_payload[:MAX_QUARANTINE_PAYLOAD_BYTES],
            "error": _bounded_text(reason.value, 2048),
            "task_id": task_id,
            "organization_id": organization_id,
            "status": "QUARANTINED",
            "quarantined_at": now,
            "replayed_at": None,
            "created_at": now,
            "updated_at": now,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            session.execute(
                postgresql_insert(MessageQuarantineRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        MessageQuarantineRow.stream_name,
                        MessageQuarantineRow.message_id,
                    ]
                )
            )
        elif dialect == "sqlite":
            session.execute(
                sqlite_insert(MessageQuarantineRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        MessageQuarantineRow.stream_name,
                        MessageQuarantineRow.message_id,
                    ]
                )
            )
        else:
            session.add(MessageQuarantineRow(**values))
        session.flush()
        row = session.scalar(
            select(MessageQuarantineRow)
            .where(
                MessageQuarantineRow.stream_name == values["stream_name"],
                MessageQuarantineRow.message_id == values["message_id"],
            )
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("quarantine row could not be persisted")
        return row

    @staticmethod
    def _require_worker_delivery_scope(
        scope: WorkerDeliveryScope, worker_id: str
    ) -> None:
        if not isinstance(scope, WorkerDeliveryScope) or scope.worker_id != worker_id:
            raise TypeError("delivery processing requires the matching WorkerDeliveryScope")

    def renew(self, claim: LeaseClaim, now: datetime) -> LeaseClaim | None:
        with self._session_factory() as session:
            current = self._db_now(session)
            state = session.scalar(
                select(CameraInferenceStateRow)
                .where(
                    CameraInferenceStateRow.organization_id == claim.organization_id,
                    CameraInferenceStateRow.running_task_id == claim.task_id,
                )
                .with_for_update()
            )
            if state is None:
                session.rollback()
                return None
            result = session.execute(
                update(InferenceTaskRow)
                .where(
                    InferenceTaskRow.task_id == claim.task_id,
                    InferenceTaskRow.organization_id == claim.organization_id,
                    InferenceTaskRow.status == TaskStatus.RUNNING.value,
                    InferenceTaskRow.lease_owner == claim.lease_owner,
                    InferenceTaskRow.fence_token == claim.fence_token,
                    InferenceTaskRow.lease_expires_at > current,
                )
                .values(
                    lease_expires_at=current + timedelta(seconds=LEASE_SECONDS), updated_at=current
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            return LeaseClaim(
                task_id=claim.task_id,
                organization_id=claim.organization_id,
                artifact_id=claim.artifact_id,
                attempt_id=claim.attempt_id,
                attempt_no=claim.attempt_no,
                fence_token=claim.fence_token,
                lease_owner=claim.lease_owner,
                lease_expires_at=current + timedelta(seconds=LEASE_SECONDS),
            )

    def record_failure(self, claim: LeaseClaim, failure: object, now: datetime) -> None:
        with self._session_factory() as session:
            current = self._db_now(session)
            state = session.scalar(
                select(CameraInferenceStateRow)
                .where(
                    CameraInferenceStateRow.organization_id == claim.organization_id,
                    CameraInferenceStateRow.running_task_id == claim.task_id,
                )
                .with_for_update()
            )
            task = session.scalar(
                select(InferenceTaskRow)
                .where(
                    InferenceTaskRow.task_id == claim.task_id,
                    InferenceTaskRow.organization_id == claim.organization_id,
                )
                .with_for_update()
            )
            if (
                task is None
                or state is None
                or task.status != TaskStatus.RUNNING.value
                or task.lease_owner != claim.lease_owner
                or task.fence_token != claim.fence_token
                or not task.lease_expires_at
                or _as_utc(task.lease_expires_at) <= current
            ):
                session.rollback()
                raise StaleLease("lease is no longer current")
            attempt = session.scalar(
                select(InferenceAttemptRow)
                .where(
                    InferenceAttemptRow.attempt_id == claim.attempt_id,
                    InferenceAttemptRow.organization_id == claim.organization_id,
                    InferenceAttemptRow.task_id == claim.task_id,
                    InferenceAttemptRow.attempt_no == claim.attempt_no,
                    InferenceAttemptRow.fence_token == claim.fence_token,
                    InferenceAttemptRow.worker_id == claim.lease_owner,
                )
                .with_for_update()
            )
            if attempt is None:
                session.rollback()
                raise StaleLease("attempt is missing")
            try:
                failure_kind = FailureKind(failure)
            except ValueError:
                failure_kind = FailureKind.RETRYABLE_INFRA
            detail = str(failure)
            attempt.finished_at = current
            attempt.outcome = "FAILED"
            attempt.error_code = failure_kind.value
            attempt.error_detail = detail
            retryable = (
                failure_kind is FailureKind.RETRYABLE_INFRA and task.attempt_count < MAX_ATTEMPTS
            )
            task.status = TaskStatus.RETRY_WAIT.value if retryable else TaskStatus.DEAD_LETTER.value
            task.next_attempt_at = (
                current + timedelta(seconds=task.attempt_count) if retryable else None
            )
            task.error_code = failure_kind.value
            task.error_detail = detail
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = current
            state.running_task_id = None
            state.version += 1
            state.updated_at = current
            session.commit()

    def release_due_retries(
        self,
        now: datetime,
        scope: SystemRecoveryScope,
        *,
        limit: int = RECOVERY_BATCH_SIZE,
    ) -> int:
        """Turn due retry waits into a fresh durable dispatch using database time."""
        self._require_system_recovery_scope(scope)
        self._require_positive_recovery_limit(limit)
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                ready_task = aliased(InferenceTaskRow)
                ready_capacity_full = (
                    select(ready_task.task_id)
                    .where(
                        ready_task.organization_id
                        == InferenceTaskRow.organization_id,
                        ready_task.camera_id == InferenceTaskRow.camera_id,
                        ready_task.status == TaskStatus.READY.value,
                    )
                    # The second matching row is enough to reject the
                    # candidate. Avoid counting the entire READY prefix.
                    .offset(1)
                    .limit(1)
                    .correlate(InferenceTaskRow)
                    .exists()
                )
                candidate_ids = session.scalars(
                    select(InferenceTaskRow.task_id)
                    .where(
                        InferenceTaskRow.status == TaskStatus.RETRY_WAIT.value,
                        InferenceTaskRow.next_attempt_at <= current,
                        ~ready_capacity_full,
                    )
                    .order_by(InferenceTaskRow.next_attempt_at, InferenceTaskRow.task_id)
                    .limit(limit)
                ).all()
                released = 0
                for task_id in candidate_ids:
                    candidate = session.scalar(
                        select(InferenceTaskRow).where(InferenceTaskRow.task_id == task_id)
                    )
                    if candidate is None:
                        continue
                    # Recovery must take the same camera-first lock as claim and
                    # finalize before acquiring a task row lock.
                    state = self._lock_camera_state(
                        session, candidate.organization_id, candidate.camera_id
                    )
                    task = session.scalar(
                        select(InferenceTaskRow)
                        .where(
                            InferenceTaskRow.task_id == task_id,
                            InferenceTaskRow.organization_id == candidate.organization_id,
                            InferenceTaskRow.status == TaskStatus.RETRY_WAIT.value,
                            InferenceTaskRow.next_attempt_at <= current,
                        )
                        .with_for_update(skip_locked=True)
                    )
                    if task is None:
                        continue
                    if self._sync_ready_count(session, state) >= 2:
                        continue
                    task.status = TaskStatus.READY.value
                    task.next_attempt_at = None
                    self._add_dispatch_outbox(session, task, current)
                    self._sync_ready_count(session, state)
                    state.version += 1
                    state.updated_at = current
                    released += 1
                session.commit()
                return released
            except BaseException:
                session.rollback()
                raise

    def redispatch_stale_ready(
        self,
        now: datetime,
        scope: SystemRecoveryScope,
        *,
        limit: int = RECOVERY_BATCH_SIZE,
    ) -> int:
        """Perform the explicit system-only cross-tenant stale-ready scan."""
        self._require_system_recovery_scope(scope)
        self._require_positive_recovery_limit(limit)
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                query = select(InferenceTaskRow).where(
                    InferenceTaskRow.status == TaskStatus.READY.value,
                    InferenceTaskRow.lease_owner.is_(None),
                    InferenceTaskRow.lease_expires_at.is_(None),
                    InferenceTaskRow.last_dispatched_at.is_not(None),
                    InferenceTaskRow.last_dispatched_at
                    <= current - timedelta(seconds=REDISPATCH_AFTER_SECONDS),
                )
                rows = session.scalars(
                    query.order_by(
                        InferenceTaskRow.last_dispatched_at, InferenceTaskRow.task_id
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
                for task in rows:
                    self._add_dispatch_outbox(session, task, current)
                session.commit()
                return len(rows)
            except BaseException:
                session.rollback()
                raise

    def expire_leases(
        self,
        now: datetime,
        scope: SystemRecoveryScope,
        *,
        limit: int = RECOVERY_BATCH_SIZE,
    ) -> int:
        self._require_system_recovery_scope(scope)
        self._require_positive_recovery_limit(limit)
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                states = session.scalars(
                    select(CameraInferenceStateRow)
                    .join(
                        InferenceTaskRow,
                        (InferenceTaskRow.task_id == CameraInferenceStateRow.running_task_id)
                        & (
                            InferenceTaskRow.organization_id
                            == CameraInferenceStateRow.organization_id
                        )
                        & (
                            InferenceTaskRow.camera_id
                            == CameraInferenceStateRow.camera_id
                        ),
                    )
                    .where(
                        InferenceTaskRow.status == TaskStatus.RUNNING.value,
                        InferenceTaskRow.lease_expires_at <= current,
                    )
                    .order_by(
                        InferenceTaskRow.lease_expires_at, InferenceTaskRow.task_id
                    )
                    .limit(limit)
                    .with_for_update(of=CameraInferenceStateRow, skip_locked=True)
                ).all()
                expired = 0
                for state in states:
                    if state.running_task_id is None:
                        continue
                    task = session.scalar(
                        select(InferenceTaskRow)
                        .where(
                            InferenceTaskRow.task_id == state.running_task_id,
                            InferenceTaskRow.organization_id == state.organization_id,
                            InferenceTaskRow.camera_id == state.camera_id,
                            InferenceTaskRow.status == TaskStatus.RUNNING.value,
                        )
                        .with_for_update()
                    )
                    if (
                        task is None
                        or task.lease_expires_at is None
                        or _as_utc(task.lease_expires_at) > current
                    ):
                        continue
                    attempt = session.scalar(
                        select(InferenceAttemptRow)
                        .where(
                            InferenceAttemptRow.task_id == task.task_id,
                            InferenceAttemptRow.organization_id == task.organization_id,
                            InferenceAttemptRow.attempt_no == task.attempt_count,
                            InferenceAttemptRow.fence_token == task.fence_token,
                        )
                        .with_for_update()
                    )
                    if attempt is not None and attempt.finished_at is None:
                        attempt.finished_at, attempt.outcome, attempt.error_code = (
                            current,
                            "LEASE_EXPIRED",
                            "LEASE_EXPIRED",
                        )
                    task.status = (
                        TaskStatus.DEAD_LETTER.value
                        if task.attempt_count >= MAX_ATTEMPTS
                        else TaskStatus.RETRY_WAIT.value
                    )
                    task.next_attempt_at = (
                        None if task.status == TaskStatus.DEAD_LETTER.value else current
                    )
                    task.lease_owner = task.lease_expires_at = None
                    task.error_code, task.error_detail, task.updated_at = (
                        "LEASE_EXPIRED",
                        "worker lease expired",
                        current,
                    )
                    state.running_task_id = None
                    state.version += 1
                    state.updated_at = current
                    expired += 1
                session.commit()
                return expired
            except BaseException:
                session.rollback()
                raise

    def release_expired_outbox_claims(
        self,
        now: datetime,
        scope: SystemRecoveryScope,
        *,
        limit: int = RECOVERY_BATCH_SIZE,
    ) -> int:
        """Clear only expired claims on durable, still-unpublished Outbox rows."""
        self._require_system_recovery_scope(scope)
        self._require_positive_recovery_limit(limit)
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                rows = session.scalars(
                    select(OutboxEventRow)
                    .where(
                        OutboxEventRow.published_at.is_(None),
                        OutboxEventRow.claim_owner.is_not(None),
                        OutboxEventRow.claim_expires_at.is_not(None),
                        OutboxEventRow.claim_expires_at <= current,
                    )
                    .order_by(OutboxEventRow.claim_expires_at, OutboxEventRow.outbox_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
                for row in rows:
                    row.claim_owner = None
                    row.claim_expires_at = None
                    row.updated_at = current
                session.commit()
                return len(rows)
            except BaseException:
                session.rollback()
                raise

    def count_quarantined_messages(
        self, now: datetime, scope: SystemRecoveryScope
    ) -> int:
        """Count compatibility records without replaying or deleting them."""
        self._require_system_recovery_scope(scope)
        with self._session_factory() as session:
            return int(
                session.scalar(select(func.count(MessageQuarantineRow.quarantine_id)))
                or 0
            )

    def expire_stale_artifact_reservations(
        self,
        now: datetime,
        scope: SystemRecoveryScope,
        *,
        limit: int = RECOVERY_BATCH_SIZE,
    ) -> int:
        """Lock camera anchors first and repair only elapsed upload reservations."""
        self._require_system_recovery_scope(scope)
        self._require_positive_recovery_limit(limit)
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                states = session.scalars(
                    select(CameraInferenceStateRow)
                    .where(
                        CameraInferenceStateRow.reservation_id.is_not(None),
                        CameraInferenceStateRow.reservation_expires_at.is_not(None),
                        CameraInferenceStateRow.reservation_expires_at <= current,
                    )
                    .order_by(
                        CameraInferenceStateRow.reservation_expires_at,
                        CameraInferenceStateRow.organization_id,
                        CameraInferenceStateRow.camera_id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
                for state in states:
                    self._expire_reservation(session, state, current)
                session.commit()
                return len(states)
            except BaseException:
                session.rollback()
                raise

    def quarantine_message(
        self,
        stream: str,
        message_id: str,
        event_id: UUID,
        event_type: str,
        schema_version: str,
        raw_payload: bytes,
        task_id: UUID,
        organization_id: UUID,
        now: datetime,
    ) -> QuarantineResult:
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                safe_stream = _bounded_text(stream, 255)
                safe_message_id = _bounded_text(message_id, 255)
                existing = session.scalar(
                    select(MessageQuarantineRow)
                    .where(
                        MessageQuarantineRow.stream_name == safe_stream,
                        MessageQuarantineRow.message_id == safe_message_id,
                    )
                )
                candidate = session.scalar(
                    select(InferenceTaskRow).where(
                        InferenceTaskRow.task_id == task_id,
                        InferenceTaskRow.organization_id == organization_id,
                    )
                )
                if candidate is None:
                    raise AdmissionRejected("TASK_NOT_READY_FOR_COMPATIBILITY_BLOCK")
                # Quarantine performs READY -> BLOCKED_COMPATIBILITY, so it
                # must join the same camera-first serialization protocol.
                state = self._lock_camera_state(session, organization_id, candidate.camera_id)
                task = session.scalar(
                    select(InferenceTaskRow)
                    .where(
                        InferenceTaskRow.task_id == task_id,
                        InferenceTaskRow.organization_id == organization_id,
                    )
                    .with_for_update()
                )
                if task is None:
                    raise AdmissionRejected("TASK_NOT_READY_FOR_COMPATIBILITY_BLOCK")
                if existing is not None:
                    existing = session.scalar(
                        select(MessageQuarantineRow)
                        .where(MessageQuarantineRow.quarantine_id == existing.quarantine_id)
                        .with_for_update()
                    )
                    if (
                        existing is not None
                        and existing.task_id == task_id
                        and existing.organization_id == organization_id
                        and task.status == TaskStatus.BLOCKED_COMPATIBILITY.value
                    ):
                        self._sync_ready_count(session, state)
                        session.commit()
                        return QuarantineResult(existing.quarantine_id, ack_after_commit=True)
                    raise AdmissionRejected("QUARANTINE_MESSAGE_NOT_CURRENTLY_BLOCKED")
                if task is None or task.status != TaskStatus.READY.value:
                    raise AdmissionRejected("TASK_NOT_READY_FOR_COMPATIBILITY_BLOCK")
                quarantine_id = uuid4()
                session.add(
                    MessageQuarantineRow(
                        quarantine_id=quarantine_id,
                        stream_name=safe_stream,
                        message_id=safe_message_id,
                        event_id=event_id,
                        event_type=_bounded_text(event_type, 255),
                        schema_version=_bounded_text(schema_version, 32),
                        raw_payload=raw_payload[:MAX_QUARANTINE_PAYLOAD_BYTES],
                        error="UNSUPPORTED_SCHEMA",
                        task_id=task_id,
                        organization_id=organization_id,
                        status="QUARANTINED",
                        quarantined_at=current,
                        replayed_at=None,
                        created_at=current,
                        updated_at=current,
                    )
                )
                task.status, task.error_code, task.error_detail, task.updated_at = (
                    TaskStatus.BLOCKED_COMPATIBILITY.value,
                    "UNSUPPORTED_SCHEMA",
                    "message quarantined",
                    current,
                )
                self._sync_ready_count(session, state)
                state.version += 1
                state.updated_at = current
                session.commit()
                return QuarantineResult(quarantine_id, ack_after_commit=True)
            except BaseException:
                session.rollback()
                raise

    def replay_compatibility(
        self, task_id: UUID, organization_id: UUID, now: datetime
    ) -> ReplayResult:
        with self._session_factory() as session:
            try:
                current = self._db_now(session)
                candidate = session.scalar(
                    select(InferenceTaskRow)
                    .where(
                        InferenceTaskRow.task_id == task_id,
                        InferenceTaskRow.organization_id == organization_id,
                    )
                )
                if candidate is None:
                    raise AdmissionRejected("TASK_NOT_BLOCKED_COMPATIBILITY")
                # Replay is a READY transition, so serialize it on the same
                # tenant/camera anchor before locking the task row.
                state = self._lock_camera_state(session, organization_id, candidate.camera_id)
                task = session.scalar(
                    select(InferenceTaskRow)
                    .where(
                        InferenceTaskRow.task_id == task_id,
                        InferenceTaskRow.organization_id == organization_id,
                        InferenceTaskRow.status == TaskStatus.BLOCKED_COMPATIBILITY.value,
                    )
                    .with_for_update()
                )
                if task is None:
                    raise AdmissionRejected("TASK_NOT_BLOCKED_COMPATIBILITY")
                if self._sync_ready_count(session, state) >= 2:
                    raise AdmissionRejected("READY_WINDOW_FULL")
                task.status, task.error_code, task.error_detail = TaskStatus.READY.value, None, None
                outbox_id = self._add_dispatch_outbox(session, task, current)
                self._sync_ready_count(session, state)
                state.version += 1
                state.updated_at = current
                quarantine = session.scalar(
                    select(MessageQuarantineRow)
                    .where(
                        MessageQuarantineRow.task_id == task_id,
                        MessageQuarantineRow.organization_id == organization_id,
                    )
                    .order_by(MessageQuarantineRow.created_at.desc())
                    .with_for_update()
                )
                if quarantine is not None:
                    quarantine.status, quarantine.replayed_at, quarantine.updated_at = (
                        "REPLAYED",
                        current,
                        current,
                    )
                command = AuditCommand(
                    organization_id,
                    "inference_task",
                    task_id,
                    "COMPATIBILITY_REPLAYED",
                    "created a new dispatch after compatibility unblock",
                    None,
                    current,
                    None,
                    None,
                )
                self._append_replay_audit(session, command)
                session.commit()
                return ReplayResult(True, task.dispatch_seq, outbox_id)
            except BaseException:
                session.rollback()
                raise

    @staticmethod
    def _add_dispatch_outbox(session: Session, task: InferenceTaskRow, current: datetime) -> UUID:
        task.dispatch_seq += 1
        task.last_dispatched_at, task.updated_at = current, current
        outbox_id = uuid4()
        session.add(
            OutboxEventRow(
                outbox_id=outbox_id,
                organization_id=task.organization_id,
                aggregate_type="inference_task",
                aggregate_id=task.task_id,
                task_id=task.task_id,
                dispatch_seq=task.dispatch_seq,
                event_type=INFERENCE_REQUEST_EVENT,
                schema_version=EVENT_SCHEMA_VERSION,
                payload={"task_id": str(task.task_id), "dispatch_seq": task.dispatch_seq},
                available_at=current,
                claim_owner=None,
                claim_expires_at=None,
                publish_attempts=0,
                published_at=None,
                last_error=None,
                created_at=current,
                updated_at=current,
            )
        )
        return outbox_id

    @staticmethod
    def _require_system_recovery_scope(scope: SystemRecoveryScope) -> None:
        if not isinstance(scope, SystemRecoveryScope):
            raise TypeError("recovery scheduler requires a SystemRecoveryScope")

    @staticmethod
    def _require_positive_recovery_limit(limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("recovery batch limit must be positive")

    @staticmethod
    def _append_replay_audit(session: Session, command: AuditCommand) -> None:
        SqlAlchemyAuditSessionRepository(session).append_under_head_lock(
            command,
            lambda sequence, previous_hash: audit_log_from_command(
                command, sequence=sequence, previous_hash=previous_hash
            ),
        )

    @staticmethod
    def _lock_camera_state(
        session: Session, organization_id: UUID, camera_id: UUID
    ) -> CameraInferenceStateRow:
        """Upsert then lock the one tenant/camera serialization anchor."""

        state = session.scalar(
            select(CameraInferenceStateRow)
            .where(
                CameraInferenceStateRow.organization_id == organization_id,
                CameraInferenceStateRow.camera_id == camera_id,
            )
            .with_for_update()
        )
        if state is not None:
            return state

        values = {
            "organization_id": organization_id,
            "camera_id": camera_id,
            "running_task_id": None,
            "ready_count": 0,
            "reservation_id": None,
            "reservation_expires_at": None,
            "last_admitted_at": None,
            "version": 0,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            session.execute(
                postgresql_insert(CameraInferenceStateRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        CameraInferenceStateRow.organization_id,
                        CameraInferenceStateRow.camera_id,
                    ]
                )
            )
        elif dialect == "sqlite":
            session.execute(
                sqlite_insert(CameraInferenceStateRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        CameraInferenceStateRow.organization_id,
                        CameraInferenceStateRow.camera_id,
                    ]
                )
            )
        else:
            session.add(CameraInferenceStateRow(**values))
        session.flush()
        state = session.scalar(
            select(CameraInferenceStateRow)
            .where(
                CameraInferenceStateRow.organization_id == organization_id,
                CameraInferenceStateRow.camera_id == camera_id,
            )
            .with_for_update()
        )
        if state is None:
            raise AdmissionRejected("CAMERA_STATE_UNAVAILABLE")
        return state

    def _db_now(self, session: Session) -> datetime:
        if self._clock is not None:
            return _as_utc(self._clock(session))
        value = session.scalar(select(func.now()))
        return _as_utc(value)

    @classmethod
    def _lock_camera_state_for_reservation(
        cls, session: Session, organization_id: UUID, reservation_id: UUID
    ) -> CameraInferenceStateRow | None:
        """Lock the camera anchor first, using the opaque reservation ID."""

        return session.scalar(
            select(CameraInferenceStateRow)
            .where(
                CameraInferenceStateRow.organization_id == organization_id,
                CameraInferenceStateRow.reservation_id == reservation_id,
            )
            .with_for_update()
        )

    @staticmethod
    def _require_session(session: Session, request: AdmissionRequest) -> None:
        row = session.scalar(
            select(InspectionSessionRow).where(
                InspectionSessionRow.session_id == request.stream_session_id,
                InspectionSessionRow.organization_id == request.organization_id,
                InspectionSessionRow.camera_id == request.camera_id,
            )
        )
        if row is None:
            raise AdmissionRejected("SESSION_NOT_FOUND")

    @staticmethod
    def _sync_ready_count(session: Session, state: CameraInferenceStateRow) -> int:
        count = session.scalar(
            select(func.count())
            .select_from(InferenceTaskRow)
            .where(
                InferenceTaskRow.organization_id == state.organization_id,
                InferenceTaskRow.camera_id == state.camera_id,
                InferenceTaskRow.status == TaskStatus.READY.value,
            )
        )
        ready_count = int(count or 0)
        state.ready_count = ready_count
        return ready_count

    @staticmethod
    def _evict_oldest_ready(
        session: Session,
        organization_id: UUID,
        camera_id: UUID,
        now: datetime,
    ) -> UUID | None:
        oldest = session.scalar(
            select(InferenceTaskRow)
            .where(
                InferenceTaskRow.organization_id == organization_id,
                InferenceTaskRow.camera_id == camera_id,
                InferenceTaskRow.status == TaskStatus.READY.value,
            )
            .order_by(InferenceTaskRow.created_at.asc(), InferenceTaskRow.task_id.asc())
            .with_for_update()
        )
        if oldest is None:
            return None
        result = session.execute(
            update(InferenceTaskRow)
            .where(
                InferenceTaskRow.task_id == oldest.task_id,
                InferenceTaskRow.organization_id == organization_id,
                InferenceTaskRow.camera_id == camera_id,
                InferenceTaskRow.status == TaskStatus.READY.value,
            )
            .values(
                status=TaskStatus.SKIPPED_BACKPRESSURE.value,
                error_code=TaskStatus.SKIPPED_BACKPRESSURE.value,
                error_detail="oldest READY task evicted for latest-frame-wins admission",
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        return oldest.task_id

    @staticmethod
    def _expire_reservation(
        session: Session, state: CameraInferenceStateRow, now: datetime
    ) -> None:
        reservation_id = state.reservation_id
        if reservation_id is not None:
            artifact = session.scalar(
                select(FrameArtifactRow)
                .where(
                    FrameArtifactRow.artifact_id == reservation_id,
                    FrameArtifactRow.organization_id == state.organization_id,
                    FrameArtifactRow.camera_id == state.camera_id,
                )
                .with_for_update()
            )
            if (
                artifact is not None
                and artifact.state == ArtifactState.PENDING.value
                and artifact.lifecycle != ArtifactLifecycle.EVIDENCE.value
            ):
                artifact.state = ArtifactState.FAILED.value
                artifact.error_code = "ADMISSION_RESERVATION_EXPIRED"
                artifact.error_detail = "upload reservation expired before completion"
                artifact.updated_at = now
        state.reservation_id = None
        state.reservation_expires_at = None
        state.version += 1
        state.updated_at = now


def _validate_request(request: AdmissionRequest) -> None:
    if request.frame_sequence < 1:
        raise AdmissionRejected("INVALID_FRAME_SEQUENCE")
    if not request.content_sha256.strip():
        raise AdmissionRejected("INVALID_CONTENT_SHA256")


def _reservation_from_artifact(artifact: FrameArtifactRow) -> AdmissionReservation:
    return AdmissionReservation(
        reservation_id=artifact.artifact_id,
        artifact_id=artifact.artifact_id,
        organization_id=artifact.organization_id,
        camera_id=artifact.camera_id,
        stream_session_id=artifact.stream_session_id,
        frame_sequence=artifact.frame_sequence,
        captured_at=_as_utc(artifact.captured_at),
        content_sha256=artifact.sha256,
    )


def _task_idempotency_key(artifact: FrameArtifactRow) -> str:
    return ":".join(
        (
            str(artifact.organization_id),
            str(artifact.camera_id),
            str(artifact.stream_session_id),
            str(artifact.frame_sequence),
            TASK_TYPE,
        )
    )


def _artifact_object_key(organization_id: UUID, artifact_id: UUID) -> str:
    return f"organizations/{organization_id}/artifacts/{artifact_id}"


def _pending_artifact(
    row: FrameArtifactRow, *, referenced: bool = False
) -> PendingArtifact:
    return PendingArtifact(
        artifact_id=row.artifact_id,
        organization_id=row.organization_id,
        object_key=row.object_key or _artifact_object_key(row.organization_id, row.artifact_id),
        sha256=row.sha256,
        content_length=row.content_length,
        referenced=referenced,
        retention_until=_as_utc(row.retention_until) if row.retention_until is not None else None,
    )


def _metadata_matches(candidate: PendingArtifact, metadata: ObjectMetadata | None) -> bool:
    if metadata is None or metadata.object_key != candidate.object_key:
        return False
    if metadata.content_length < 0 or (
        candidate.content_length is not None
        and metadata.content_length != candidate.content_length
    ):
        return False
    return metadata.sha256 is not None and metadata.sha256.lower() == candidate.sha256.lower()


def _task_record(row: InferenceTaskRow) -> TaskRecord:
    return TaskRecord(
        task_id=row.task_id,
        task_type=TASK_TYPE,
        idempotency_key=row.idempotency_key,
        payload={
            "task_id": str(row.task_id),
            "artifact_id": str(row.artifact_id),
            "dispatch_seq": row.dispatch_seq,
        },
        status=TaskStatus(row.status),
        attempt_count=row.attempt_count,
        created_at=_as_utc(row.created_at),
        next_attempt_at=_optional_utc(row.next_attempt_at),
        last_error=row.error_detail,
        frame_status=None,
        published_at=None,
        organization_id=row.organization_id,
        camera_id=row.camera_id,
        artifact_id=row.artifact_id,
        dispatch_seq=row.dispatch_seq,
        last_dispatched_at=_optional_utc(row.last_dispatched_at),
        lease_owner=row.lease_owner,
        fence_token=row.fence_token,
        lease_expires_at=_optional_utc(row.lease_expires_at),
        error_code=row.error_code,
        error_detail=row.error_detail,
        updated_at=_optional_utc(row.updated_at),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


def _bounded_text(value: str, limit: int) -> str:
    # PostgreSQL text rejects U+0000 even when it arrived as a JSON escape.
    # Canonicalize before both lookup and insert so poison bytes cannot leave
    # a delivery transaction in a failed/Pending state.
    text_value = str(value).replace("\x00", "\ufffd")
    return text_value[:limit]


def _is_terminal_status(status: str) -> bool:
    return status in {
        TaskStatus.SUCCEEDED.value,
        TaskStatus.DEAD_LETTER.value,
        TaskStatus.BLOCKED_COMPATIBILITY.value,
        TaskStatus.SKIPPED_STALE.value,
        TaskStatus.SKIPPED_BACKPRESSURE.value,
    }
