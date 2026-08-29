from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from odp_schemas.events import InspectionAlertCreated
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from odp_api.adapters.persistence.models import (
    AlertRow,
    DefectCaseRow,
    InspectionAlertFeedRow,
    InspectionEventRow,
)
from odp_api.adapters.persistence.repositories import SqlAlchemyAuditSessionRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    DefectEpisodeRow,
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
    PublishedInferenceResultRow,
)
from odp_api.modules.audit.models import AuditCommand, audit_log_from_command
from odp_api.modules.inspection.effects import PublishConflict, PublishedEffect
from odp_api.modules.tasks.models import TaskStatus
from odp_api.ports.tasks import StaleLease


def claim_defect_episode(
    session: Session,
    organization_id,
    camera_id,
    defect_type: str,
    spatial_zone: str,
    now: datetime,
) -> DefectEpisodeRow:
    """Create-or-lock one tenant/camera/defect episode in a caller transaction."""

    statement = select(DefectEpisodeRow).where(
        DefectEpisodeRow.organization_id == organization_id,
        DefectEpisodeRow.camera_id == camera_id,
        DefectEpisodeRow.defect_type == defect_type,
        DefectEpisodeRow.spatial_zone == spatial_zone,
    )
    row = session.scalar(statement.with_for_update())
    if row is not None:
        return row

    values = {
        "episode_id": uuid4(),
        "organization_id": organization_id,
        "camera_id": camera_id,
        "defect_type": defect_type,
        "spatial_zone": spatial_zone,
        "current_case_id": None,
        "episode_expires_at": now,
        "created_at": now,
        "updated_at": now,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        session.execute(
            insert(DefectEpisodeRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    DefectEpisodeRow.organization_id,
                    DefectEpisodeRow.camera_id,
                    DefectEpisodeRow.defect_type,
                    DefectEpisodeRow.spatial_zone,
                ]
            )
        )
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        session.execute(
            insert(DefectEpisodeRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    DefectEpisodeRow.organization_id,
                    DefectEpisodeRow.camera_id,
                    DefectEpisodeRow.defect_type,
                    DefectEpisodeRow.spatial_zone,
                ]
            )
        )
    else:
        session.add(DefectEpisodeRow(**values))
    session.flush()
    row = session.scalar(statement.with_for_update())
    if row is None:
        raise RuntimeError("defect episode could not be claimed")
    return row


class SqlAlchemyInspectionEffects:
    def __init__(
        self,
        session_factory,
        *,
        clock: Callable[[Session], datetime] | None = None,
    ):
        self._session_factory = session_factory
        self._clock = clock

    def publish(self, command):
        c = command.claim
        with self._session_factory() as s:
            try:
                # Camera is the serialization anchor, then artifact, task, attempt.
                task = s.scalar(
                    select(InferenceTaskRow).where(InferenceTaskRow.task_id == c.task_id)
                )
                if task is None or task.organization_id != c.organization_id:
                    raise StaleLease("tenant mismatch")
                state = s.get(
                    CameraInferenceStateRow,
                    (c.organization_id, task.camera_id),
                    with_for_update=True,
                )
                if state is None:
                    raise StaleLease("camera anchor missing")
                task = s.scalar(
                    select(InferenceTaskRow)
                    .where(
                        InferenceTaskRow.task_id == c.task_id,
                        InferenceTaskRow.organization_id == c.organization_id,
                    )
                    .with_for_update()
                )
                attempt = s.scalar(
                    select(InferenceAttemptRow)
                    .where(
                        InferenceAttemptRow.attempt_id == c.attempt_id,
                        InferenceAttemptRow.organization_id == c.organization_id,
                        InferenceAttemptRow.task_id == c.task_id,
                    )
                    .with_for_update()
                )
                if task is None or task.artifact_id != c.artifact_id:
                    raise StaleLease("artifact does not belong to task")
                artifact = s.scalar(
                    select(FrameArtifactRow)
                    .where(
                        FrameArtifactRow.artifact_id == c.artifact_id,
                        FrameArtifactRow.organization_id == c.organization_id,
                        FrameArtifactRow.camera_id == task.camera_id,
                        FrameArtifactRow.state == "AVAILABLE",
                    )
                    .with_for_update()
                )
                stream_session = None
                if artifact is not None:
                    stream_session = s.scalar(
                        select(InspectionSessionRow)
                        .where(
                            InspectionSessionRow.session_id == artifact.stream_session_id,
                            InspectionSessionRow.organization_id == c.organization_id,
                            InspectionSessionRow.camera_id == task.camera_id,
                        )
                        .with_for_update()
                    )
                if (
                    attempt is None
                    or artifact is None
                    or stream_session is None
                ):
                    raise StaleLease("invalid artifact chain")
                if (
                    attempt.attempt_no != c.attempt_no
                    or attempt.fence_token != c.fence_token
                    or attempt.worker_id != c.lease_owner
                ):
                    raise StaleLease("attempt identity mismatch")
                if command.frame_sha256 != artifact.sha256:
                    raise PublishConflict("frame hash differs from artifact")
                now = self._db_now(s)
                old = s.scalar(
                    select(PublishedInferenceResultRow)
                    .where(
                        PublishedInferenceResultRow.task_id == task.task_id,
                        PublishedInferenceResultRow.organization_id == c.organization_id,
                    )
                    .with_for_update()
                )
                contract = command.execution_contract
                if old:
                    stored = (
                        old.model_release,
                        old.model_sha256,
                        old.onnxruntime_version,
                        old.execution_provider,
                        tuple(old.actual_input_shape),
                        old.preprocessing_version,
                        old.postprocessing_version,
                        old.confidence_threshold,
                        old.iou_threshold,
                        old.nms_mode,
                        old.nms_in_model,
                        old.class_map_version,
                        old.frame_sha256,
                        old.detections,
                        old.stage_durations,
                    )
                    incoming = (
                        contract.model_release,
                        contract.model_sha256,
                        contract.onnxruntime_version,
                        contract.execution_provider,
                        tuple(contract.actual_input_shape),
                        contract.preprocessing_version,
                        contract.postprocessing_version,
                        contract.confidence_threshold,
                        contract.iou_threshold,
                        contract.nms_mode,
                        contract.nms_in_model,
                        contract.class_map_version,
                        command.frame_sha256,
                        list(command.detections),
                        dict(command.stage_durations),
                    )
                    if stored != incoming:
                        raise PublishConflict("payload differs")
                    ev = s.scalar(
                        select(InspectionEventRow).where(
                            InspectionEventRow.source_result_id == old.result_id,
                            InspectionEventRow.organization_id == c.organization_id,
                        )
                    )
                    return PublishedEffect(
                        old.result_id,
                        ev.event_id if ev else None,
                        ev.case_id if ev else None,
                        self._alert_id(s, c.organization_id, ev.event_id) if ev else None,
                    )
                if (
                    task.status != TaskStatus.RUNNING.value
                    or task.lease_owner != c.lease_owner
                    or task.fence_token != c.fence_token
                    or task.lease_expires_at is None
                    or self._utc(task.lease_expires_at) <= now
                ):
                    raise StaleLease("stale lease")
                result_id = uuid4()
                s.add(
                    PublishedInferenceResultRow(
                        result_id=result_id,
                        organization_id=c.organization_id,
                        task_id=task.task_id,
                        attempt_id=attempt.attempt_id,
                        artifact_id=artifact.artifact_id,
                        model_release=contract.model_release,
                        model_sha256=contract.model_sha256,
                        onnxruntime_version=contract.onnxruntime_version,
                        execution_provider=contract.execution_provider,
                        actual_input_shape=list(contract.actual_input_shape),
                        preprocessing_version=contract.preprocessing_version,
                        postprocessing_version=contract.postprocessing_version,
                        confidence_threshold=contract.confidence_threshold,
                        iou_threshold=contract.iou_threshold,
                        nms_mode=contract.nms_mode,
                        nms_in_model=contract.nms_in_model,
                        class_map_version=contract.class_map_version,
                        frame_sha256=command.frame_sha256,
                        input_frame_sha256=command.frame_sha256,
                        detections=list(command.detections),
                        stage_durations=dict(command.stage_durations),
                        correlation_id=command.correlation_id,
                        published_at=now,
                        created_at=now,
                    )
                )
                attempt.finished_at, attempt.outcome = now, "SUCCEEDED"
                task.status, task.lease_owner, task.lease_expires_at = (
                    TaskStatus.SUCCEEDED.value,
                    None,
                    None,
                )
                task.updated_at = now
                state.running_task_id = None
                state.version += 1
                state.updated_at = now
                event = case = outbox = None
                if command.detections:
                    d = command.detections[0]
                    typ, zone = (
                        str(d.get("defect_type", "UNKNOWN")),
                        str(d.get("spatial_zone") or "GLOBAL"),
                    )
                    ep = claim_defect_episode(
                        s, c.organization_id, task.camera_id, typ, zone, now
                    )
                    if ep.current_case_id is None or self._utc(ep.episode_expires_at) <= now:
                        case = DefectCaseRow(
                            case_id=uuid4(),
                            organization_id=c.organization_id,
                            status="PENDING_CONFIRMATION",
                            line_id=stream_session.line_id,
                            updated_at=now,
                        )
                        s.add(case)
                        s.flush()
                        ep.current_case_id, ep.episode_expires_at, ep.updated_at = (
                            case.case_id,
                            now + timedelta(minutes=5),
                            now,
                        )
                    else:
                        case = s.scalar(
                            select(DefectCaseRow)
                            .where(
                                DefectCaseRow.case_id == ep.current_case_id,
                                DefectCaseRow.organization_id == c.organization_id,
                            )
                            .with_for_update()
                        )
                        if case is None:
                            raise StaleLease("episode case is missing")
                        if case.line_id is None:
                            case.line_id = stream_session.line_id
                        elif case.line_id != stream_session.line_id:
                            raise StaleLease("case line mismatch")
                    eid = uuid4()
                    event = InspectionEventRow(
                        event_id=eid,
                        case_id=case.case_id,
                        organization_id=c.organization_id,
                        camera_id=task.camera_id,
                        line_id=stream_session.line_id,
                        occurred_at=now,
                        defect_class=typ,
                        confidence=float(d.get("confidence", 0)),
                        model_release=contract.model_release,
                        preprocessing_parameters=[list(x) for x in ()],
                        threshold=contract.confidence_threshold,
                        input_frame_sha256=command.frame_sha256,
                        source_result_id=result_id,
                        evidence_artifact_id=artifact.artifact_id,
                    )
                    s.add(event)
                    artifact.lifecycle = "EVIDENCE"
                    feed = InspectionAlertFeedRow(
                        event_id=eid,
                        organization_id=c.organization_id,
                        line_id=stream_session.line_id,
                        payload={},
                        created_at=now,
                    )
                    s.add(feed)
                    s.flush()
                    canonical_payload = InspectionAlertCreated(
                        alert_id=eid,
                        organization_id=c.organization_id,
                        case_id=case.case_id,
                        event_id=eid,
                        camera_id=task.camera_id,
                        line_id=stream_session.line_id,
                        defect_type=typ,
                        severity=str(d.get("severity", "UNKNOWN")),
                        confidence=float(d.get("confidence", 0)),
                        occurred_at=now,
                        business_cursor=str(feed.cursor),
                    ).model_dump(mode="json")
                    feed_payload = {**canonical_payload, "defect_class": typ}
                    s.add(
                        AlertRow(
                            alert_id=eid,
                            organization_id=c.organization_id,
                            event_id=eid,
                            line_id=stream_session.line_id,
                            alert_type="INSPECTION",
                            payload=feed_payload,
                            created_at=now,
                        )
                    )
                    feed.payload = feed_payload
                    outbox = OutboxEventRow(
                        outbox_id=uuid4(),
                        organization_id=c.organization_id,
                        aggregate_type="inspection_event",
                        aggregate_id=eid,
                        task_id=task.task_id,
                        dispatch_seq=task.dispatch_seq,
                        event_type="inspection.alert.created.v1",
                        schema_version=1,
                        payload=canonical_payload,
                        available_at=now,
                        publish_attempts=0,
                        created_at=now,
                        updated_at=now,
                    )
                    s.add(outbox)
                    audit = AuditCommand(
                        c.organization_id,
                        "inspection_event",
                        eid,
                        "INSPECTION_PUBLISHED",
                        "{}",
                        None,
                        now,
                        command.correlation_id,
                        None,
                    )
                    SqlAlchemyAuditSessionRepository(s).append_under_head_lock(
                        audit,
                        lambda sequence, previous_hash: audit_log_from_command(
                            audit, sequence=sequence, previous_hash=previous_hash
                        ),
                    )
                s.flush()
                s.commit()
                return PublishedEffect(
                    result_id,
                    event.event_id if event else None,
                    case.case_id if case else None,
                    outbox.outbox_id if outbox else None,
                )
            except BaseException:
                s.rollback()
                raise

    @staticmethod
    def _utc(value):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _db_now(self, session: Session) -> datetime:
        if self._clock is not None:
            return self._utc(self._clock(session))
        return self._utc(session.scalar(select(func.now())))

    @staticmethod
    def _alert_id(s, organization_id, event_id):
        return s.scalar(
            select(OutboxEventRow.outbox_id).where(
                OutboxEventRow.organization_id == organization_id,
                OutboxEventRow.aggregate_id == event_id,
            )
        )
