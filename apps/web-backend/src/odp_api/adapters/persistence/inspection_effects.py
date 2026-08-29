from datetime import UTC, timedelta
from uuid import uuid4

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
    OutboxEventRow,
    PublishedInferenceResultRow,
)
from odp_api.modules.audit.models import AuditCommand, audit_log_from_command
from odp_api.modules.inspection.effects import PublishConflict, PublishedEffect
from odp_api.modules.tasks.models import TaskStatus
from odp_api.ports.tasks import StaleLease
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError


class SqlAlchemyInspectionEffects:
    def __init__(self, session_factory):
        self._session_factory = session_factory

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
                    .where(InferenceAttemptRow.attempt_id == c.attempt_id)
                    .with_for_update()
                )
                artifact = s.get(FrameArtifactRow, c.artifact_id, with_for_update=True)
                if (
                    attempt is None
                    or artifact is None
                    or attempt.task_id != task.task_id
                    or attempt.organization_id != c.organization_id
                ):
                    raise StaleLease("invalid attempt")
                if (
                    attempt.attempt_no != c.attempt_no
                    or attempt.fence_token != c.fence_token
                    or attempt.worker_id != c.lease_owner
                ):
                    raise StaleLease("attempt identity mismatch")
                now = s.scalar(select(func.now())).replace(tzinfo=UTC)
                old = s.scalar(
                    select(PublishedInferenceResultRow)
                    .where(PublishedInferenceResultRow.task_id == task.task_id)
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
                            InspectionEventRow.source_result_id == old.result_id
                        )
                    )
                    return PublishedEffect(
                        old.result_id,
                        ev.event_id if ev else None,
                        ev.case_id if ev else None,
                        self._alert_id(s, ev.event_id) if ev else None,
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
                state.running_task_id = None
                event = case = outbox = None
                if command.detections:
                    d = command.detections[0]
                    typ, zone = (
                        str(d.get("defect_type", "UNKNOWN")),
                        str(d.get("spatial_zone") or "GLOBAL"),
                    )
                    ep = self._claim_episode(s, c.organization_id, task.camera_id, typ, zone, now)
                    if ep is not None:
                        ep = s.scalar(
                        select(DefectEpisodeRow)
                        .where(
                            DefectEpisodeRow.organization_id == c.organization_id,
                            DefectEpisodeRow.camera_id == task.camera_id,
                            DefectEpisodeRow.defect_type == typ,
                            DefectEpisodeRow.spatial_zone == zone,
                        )
                        .with_for_update()
                    )
                    if ep is None or self._utc(ep.episode_expires_at) <= now:
                        case = DefectCaseRow(
                            case_id=uuid4(),
                            organization_id=c.organization_id,
                            status="PENDING_CONFIRMATION",
                            updated_at=now,
                        )
                        s.add(case)
                        s.flush()
                        if ep is None:
                            ep = DefectEpisodeRow(
                                episode_id=uuid4(),
                                organization_id=c.organization_id,
                                camera_id=task.camera_id,
                                defect_type=typ,
                                spatial_zone=zone,
                                created_at=now,
                                updated_at=now,
                            )
                            s.add(ep)
                        ep.current_case_id, ep.episode_expires_at, ep.updated_at = (
                            case.case_id,
                            now + timedelta(minutes=5),
                            now,
                        )
                    else:
                        case = s.get(DefectCaseRow, ep.current_case_id)
                    eid = uuid4()
                    event = InspectionEventRow(
                        event_id=eid,
                        case_id=case.case_id,
                        organization_id=c.organization_id,
                        camera_id=task.camera_id,
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
                    s.add(
                        AlertRow(
                            alert_id=eid,
                            organization_id=c.organization_id,
                            event_id=eid,
                            alert_type="INSPECTION",
                            payload={
                                "event_id": str(eid),
                                "organization_id": str(c.organization_id),
                                "camera_id": str(task.camera_id),
                                "occurred_at": now.isoformat(),
                                "defect_class": typ,
                                "confidence": float(d.get("confidence", 0)),
                            },
                            created_at=now,
                        )
                    )
                    s.add(
                        InspectionAlertFeedRow(
                            event_id=eid,
                            organization_id=c.organization_id,
                            payload={
                                "event_id": str(eid),
                                "organization_id": str(c.organization_id),
                                "camera_id": str(task.camera_id),
                                "occurred_at": now.isoformat(),
                                "defect_class": typ,
                                "confidence": float(d.get("confidence", 0)),
                            },
                            created_at=now,
                        )
                    )
                    outbox = OutboxEventRow(
                        outbox_id=uuid4(),
                        organization_id=c.organization_id,
                        aggregate_type="inspection_event",
                        aggregate_id=eid,
                        task_id=task.task_id,
                        dispatch_seq=task.dispatch_seq,
                        event_type="inspection.alert.v1",
                        schema_version="v1",
                        payload={"event_id": str(eid)},
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
    def _claim_episode(session, organization_id, camera_id, defect_type, spatial_zone, now):
        existing = session.scalar(select(DefectEpisodeRow).where(DefectEpisodeRow.organization_id==organization_id, DefectEpisodeRow.camera_id==camera_id, DefectEpisodeRow.defect_type==defect_type, DefectEpisodeRow.spatial_zone==spatial_zone).with_for_update())
        if existing is not None:
            return existing
        try:
            with session.begin_nested():
                session.add(DefectEpisodeRow(episode_id=uuid4(), organization_id=organization_id, camera_id=camera_id, defect_type=defect_type, spatial_zone=spatial_zone, episode_expires_at=now, created_at=now, updated_at=now))
                session.flush()
        except IntegrityError:
            pass
        return session.scalar(select(DefectEpisodeRow).where(DefectEpisodeRow.organization_id==organization_id, DefectEpisodeRow.camera_id==camera_id, DefectEpisodeRow.defect_type==defect_type, DefectEpisodeRow.spatial_zone==spatial_zone).with_for_update())

    @staticmethod
    def _utc(value):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _alert_id(s, event_id):
        return s.scalar(
            select(OutboxEventRow.outbox_id).where(OutboxEventRow.aggregate_id == event_id)
        )
