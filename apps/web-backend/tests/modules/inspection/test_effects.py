"""Business effects published by the fenced inspection boundary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select

from odp_api.adapters.persistence.inspection_effects import SqlAlchemyInspectionEffects
from odp_api.adapters.persistence.models import (
    AlertRow,
    AuditLogRow,
    Base,
    DefectCaseRow,
    InspectionAlertFeedRow,
    InspectionEventRow,
)
from odp_api.adapters.persistence.repositories import SqlAlchemyAuditSessionRepository
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    DefectEpisodeRow,
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
    PublishedInferenceResultRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.inspection.effects import InspectionEffectService, PublishConflict
from odp_api.modules.tasks.commands import (
    InferenceExecutionContract,
    PublishInferenceCommand,
)
from odp_api.modules.tasks.models import TaskStatus
from odp_api.ports.tasks import StaleLease


def _command(claim, now, detections=()):
    return PublishInferenceCommand(
        claim,
        InferenceExecutionContract(
            "model-1",
            "b" * 64,
            "1",
            "cpu",
            (1, 3, 32, 32),
            "pre",
            "post",
            0.5,
            0.5,
            "hard",
            False,
            "classes",
        ),
        "a" * 64,
        tuple(detections),
        (("model", 3.0),),
        uuid4(),
        now,
    )


def _running(sessions, now, *, camera=None, organization=None, line_id=None):
    org, camera, artifact, task = organization or uuid4(), camera or uuid4(), uuid4(), uuid4()
    stream_session, line_id = uuid4(), line_id or uuid4()
    with sessions.begin() as session:
        session.add_all(
            (
                InspectionSessionRow(
                    session_id=stream_session,
                    organization_id=org,
                    camera_id=camera,
                    line_id=line_id,
                    source_type="TEST",
                    sanitized_uri="rtsp://test.invalid/camera",
                    status="RUNNING",
                    idempotency_key=str(stream_session),
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                FrameArtifactRow(
                    artifact_id=artifact,
                    organization_id=org,
                    camera_id=camera,
                    stream_session_id=stream_session,
                    frame_sequence=1,
                    captured_at=now,
                    sha256="a" * 64,
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=task,
                    organization_id=org,
                    camera_id=camera,
                    artifact_id=artifact,
                    idempotency_key=str(task),
                    status="READY",
                    dispatch_seq=1,
                    attempt_count=0,
                    fence_token=0,
                    created_at=now,
                    updated_at=now,
                ),
            )
        )
        if session.get(CameraInferenceStateRow, (org, camera)) is None:
            session.add(
                CameraInferenceStateRow(
                    organization_id=org,
                    camera_id=camera,
                    running_task_id=None,
                    ready_count=1,
                    version=0,
                )
            )
        else:
            session.get(CameraInferenceStateRow, (org, camera)).ready_count = 1
    claim = SqlAlchemyTaskControlRepository(sessions).claim(task, org, "worker", now)
    assert claim is not None
    return org, camera, artifact, task, claim


@pytest.fixture
def runtime(tmp_path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'effects.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessions
    finally:
        engine.dispose()


def _service(sessions, *, hook=None):
    return InspectionEffectService(SqlAlchemyInspectionEffects(sessions, clock=hook))


def test_product_scope_is_snapshotted_and_changes_rotate_case(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    org, camera, line = uuid4(), uuid4(), uuid4()
    effects = []
    for category in ("外壳注塑件", "外壳注塑件", "金属件", None):
        _, _, artifact, _, claim = _running(runtime, now, camera=camera,
                                           organization=org, line_id=line)
        with runtime.begin() as session:
            row = session.get(FrameArtifactRow, artifact)
            session.get(InspectionSessionRow, row.stream_session_id).product_category = category
        effects.append(_service(runtime, hook=lambda _: now).publish(_command(
            claim, now, ({"defect_type": "scratch", "confidence": 0.95},))))
    assert effects[0].case_id == effects[1].case_id
    assert effects[1].case_id != effects[2].case_id
    assert effects[2].case_id != effects[3].case_id
    with runtime() as session:
        assert [session.get(DefectCaseRow, effect.case_id).product_category for effect in effects] == [
            "外壳注塑件", "外壳注塑件", "金属件", None,
        ]


def test_no_defect_publishes_result_and_closes_fenced_execution_without_business_effects(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    org, camera, artifact, task, claim = _running(runtime, now)
    effect = _service(runtime).publish(_command(claim, now))
    assert effect.event_id is effect.case_id is effect.alert_outbox_id is None
    with runtime() as session:
        assert session.get(PublishedInferenceResultRow, effect.result_id).task_id == task
        assert session.get(InferenceTaskRow, task).status == TaskStatus.SUCCEEDED.value
        assert session.get(CameraInferenceStateRow, (org, camera)).running_task_id is None
        assert session.get(FrameArtifactRow, artifact).lifecycle == "PROCESSING"
        assert session.scalar(select(func.count()).select_from(InspectionEventRow)) == 0
        assert session.scalar(select(func.count()).select_from(DefectCaseRow)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) == 0


def test_defect_creates_event_case_evidence_alert_outbox_and_audit_in_one_effect(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    org, _camera, artifact, _task, claim = _running(runtime, now)
    effect = _service(runtime, hook=lambda _session: now).publish(
        _command(claim, now, ({"defect_type": "scratch", "confidence": 0.91, "spatial_zone": "A"},))
    )
    assert effect.event_id and effect.case_id and effect.alert_outbox_id
    with runtime() as session:
        evidence = session.get(FrameArtifactRow, artifact)
        assert evidence.lifecycle == "EVIDENCE"
        retention_until = evidence.retention_until
        if retention_until.tzinfo is None:
            retention_until = retention_until.replace(tzinfo=UTC)
        assert retention_until == now + timedelta(days=90)
        assert session.get(InspectionEventRow, effect.event_id).case_id == effect.case_id
        assert session.get(AlertRow, effect.event_id) is not None
        assert session.scalar(select(func.count()).select_from(InspectionAlertFeedRow)) == 1
        assert session.get(OutboxEventRow, effect.alert_outbox_id).aggregate_id == effect.event_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLogRow)
                .where(AuditLogRow.organization_id == org)
            )
            == 1
        )


def test_defect_propagates_authoritative_line_and_canonical_alert_envelope(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    org, camera, artifact, _task, claim = _running(runtime, now)
    effect = _service(runtime).publish(
        _command(
            claim,
            now,
            (
                {
                    "defect_type": "scratch",
                    "severity": "HIGH",
                    "confidence": 0.91,
                    "spatial_zone": "A",
                },
            ),
        )
    )

    with runtime() as session:
        artifact_row = session.get(FrameArtifactRow, artifact)
        stream_session = session.get(InspectionSessionRow, artifact_row.stream_session_id)
        event = session.get(InspectionEventRow, effect.event_id)
        case = session.get(DefectCaseRow, effect.case_id)
        alert = session.get(AlertRow, effect.event_id)
        feed = session.scalar(
            select(InspectionAlertFeedRow).where(
                InspectionAlertFeedRow.organization_id == org,
                InspectionAlertFeedRow.event_id == effect.event_id,
            )
        )
        outbox = session.get(OutboxEventRow, effect.alert_outbox_id)

        assert stream_session.line_id is not None
        assert event.line_id == stream_session.line_id
        assert case.line_id == stream_session.line_id
        assert alert.line_id == stream_session.line_id
        assert feed.line_id == stream_session.line_id
        assert alert.payload["line_id"] == str(stream_session.line_id)
        assert feed.payload["line_id"] == str(stream_session.line_id)
        assert outbox.event_type == "inspection.alert.created.v1"
        assert outbox.schema_version == 1
        occurred_at = event.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        assert outbox.payload == {
            "alert_id": str(effect.event_id),
            "organization_id": str(org),
            "case_id": str(effect.case_id),
            "event_id": str(effect.event_id),
            "camera_id": str(camera),
            "line_id": str(stream_session.line_id),
            "defect_type": "scratch",
            "severity": "HIGH",
            "confidence": 0.91,
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
            "business_cursor": str(feed.cursor),
        }


def test_exact_duplicate_returns_original_ids_but_conflicting_payload_is_rejected(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, _artifact, _task, claim = _running(runtime, now)
    command = _command(claim, now, ({"defect_type": "scratch", "confidence": 0.91},))
    service = _service(runtime)
    first = service.publish(command)
    assert service.publish(command) == first
    with pytest.raises(PublishConflict):
        service.publish(replace(command, frame_sha256="c" * 64))
    with runtime() as session:
        assert session.scalar(select(func.count()).select_from(InspectionEventRow)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) == 1


@pytest.mark.parametrize(
    "field",
    [
        "model_release",
        "model_sha256",
        "onnxruntime_version",
        "execution_provider",
        "actual_input_shape",
        "preprocessing_version",
        "postprocessing_version",
        "confidence_threshold",
        "iou_threshold",
        "nms_mode",
        "nms_in_model",
        "class_map_version",
    ],
)
def test_every_execution_field_conflict_is_rejected(runtime, field):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, _artifact, _task, claim = _running(runtime, now)
    command = _command(claim, now, ({"defect_type": "scratch", "confidence": 0.91},))
    service = _service(runtime)
    service.publish(command)
    values = {
        "model_release": "other",
        "model_sha256": "c" * 64,
        "onnxruntime_version": "other",
        "execution_provider": "other",
        "actual_input_shape": (9,),
        "preprocessing_version": "other",
        "postprocessing_version": "other",
        "confidence_threshold": 0.6,
        "iou_threshold": 0.6,
        "nms_mode": "other",
        "nms_in_model": True,
        "class_map_version": "other",
    }
    mutated = replace(
        command, execution_contract=replace(command.execution_contract, **{field: values[field]})
    )
    with pytest.raises(PublishConflict):
        service.publish(mutated)


def test_stale_or_wrong_tenant_claim_has_zero_effects(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, _artifact, _task, claim = _running(runtime, now)
    command = _command(claim, now, ({"defect_type": "scratch", "confidence": 0.91},))
    with runtime.begin() as session:
        session.get(InferenceTaskRow, claim.task_id).lease_expires_at = now - timedelta(seconds=1)
    with pytest.raises(StaleLease):
        _service(runtime).publish(command)
    with runtime() as session:
        assert session.scalar(select(func.count()).select_from(PublishedInferenceResultRow)) == 0
        assert session.scalar(select(func.count()).select_from(DefectCaseRow)) == 0


def test_initial_task_locator_is_tenant_scoped_at_the_database_boundary(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, _artifact, _task, claim = _running(runtime, now)
    statements = []
    with runtime() as session:
        engine = session.get_bind()

    def capture_task_select(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lower()
        if normalized.lstrip().startswith("select") and "inference_tasks" in normalized:
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture_task_select)
    try:
        with pytest.raises(StaleLease):
            _service(runtime).publish(_command(replace(claim, organization_id=uuid4()), now))
    finally:
        event.remove(engine, "before_cursor_execute", capture_task_select)

    assert statements
    statement = " ".join(statements[0].split())
    assert " where " in statement
    assert "organization_id" in statement.split(" where ", 1)[1]


def test_publication_rejects_claim_artifact_not_owned_by_task(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    org, camera, artifact, _task, claim = _running(runtime, now)
    other_artifact = uuid4()
    with runtime.begin() as session:
        original = session.get(FrameArtifactRow, artifact)
        session.add(
            FrameArtifactRow(
                artifact_id=other_artifact,
                organization_id=org,
                camera_id=camera,
                stream_session_id=original.stream_session_id,
                frame_sequence=2,
                captured_at=now,
                sha256="a" * 64,
                state="AVAILABLE",
                lifecycle="PROCESSING",
                created_at=now,
                updated_at=now,
            )
        )

    with pytest.raises(StaleLease, match="artifact"):
        _service(runtime).publish(_command(replace(claim, artifact_id=other_artifact), now))

    with runtime() as session:
        assert session.scalar(select(func.count()).select_from(PublishedInferenceResultRow)) == 0


def test_publication_rejects_frame_hash_different_from_artifact(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, _artifact, _task, claim = _running(runtime, now)

    with pytest.raises(PublishConflict, match="frame hash"):
        _service(runtime).publish(replace(_command(claim, now), frame_sha256="c" * 64))

    with runtime() as session:
        assert session.scalar(select(func.count()).select_from(PublishedInferenceResultRow)) == 0


@pytest.mark.parametrize("broken_scope", ["artifact_tenant", "artifact_camera", "artifact_state", "session_camera"])
def test_publication_rejects_broken_tenant_camera_session_artifact_chain(runtime, broken_scope):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, artifact, _task, claim = _running(runtime, now)
    with runtime.begin() as session:
        artifact_row = session.get(FrameArtifactRow, artifact)
        stream_session = session.get(InspectionSessionRow, artifact_row.stream_session_id)
        if broken_scope == "artifact_tenant":
            artifact_row.organization_id = uuid4()
        elif broken_scope == "artifact_camera":
            artifact_row.camera_id = uuid4()
        elif broken_scope == "artifact_state":
            artifact_row.state = "PENDING"
        else:
            stream_session.camera_id = uuid4()

    with pytest.raises(StaleLease, match="artifact|session"):
        _service(runtime).publish(_command(claim, now))

    with runtime() as session:
        assert session.scalar(select(func.count()).select_from(PublishedInferenceResultRow)) == 0


def test_episode_reuses_open_case_and_expiry_creates_a_new_case(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    org, camera, first_artifact, _task, claim = _running(runtime, now)
    first = _service(runtime).publish(
        _command(claim, now, ({"defect_type": "scratch", "confidence": 0.91},))
    )
    with runtime() as session:
        first_artifact_row = session.get(FrameArtifactRow, first_artifact)
        first_line = session.get(
            InspectionSessionRow, first_artifact_row.stream_session_id
        ).line_id
    _org2, _camera2, _artifact2, _task2, claim2 = _running(
        runtime,
        now,
        camera=camera,
        organization=org,
        line_id=first_line,
    )
    second = _service(runtime).publish(
        _command(claim2, now, ({"defect_type": "scratch", "confidence": 0.92},))
    )
    assert second.case_id == first.case_id
    with runtime.begin() as session:
        session.scalar(
            select(DefectEpisodeRow).where(DefectEpisodeRow.organization_id == org)
        ).episode_expires_at = now - timedelta(seconds=1)
    _org3, _camera3, _artifact3, _task3, claim3 = _running(
        runtime, now, camera=camera, organization=org
    )
    third = _service(runtime).publish(
        _command(claim3, now, ({"defect_type": "scratch", "confidence": 0.93},))
    )
    assert third.case_id != first.case_id


def test_episode_shell_is_claimed_before_case_creation(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    org, camera, _artifact, _task, claim = _running(runtime, now)
    with runtime.begin() as session:
        session.add(
            DefectEpisodeRow(
                episode_id=uuid4(),
                organization_id=org,
                camera_id=camera,
                defect_type="scratch",
                spatial_zone="GLOBAL",
                current_case_id=None,
                episode_expires_at=now + timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
        )
    effect = _service(runtime).publish(
        _command(claim, now, ({"defect_type": "scratch", "confidence": 0.9},))
    )
    with runtime() as session:
        episode = session.scalar(
            select(DefectEpisodeRow).where(DefectEpisodeRow.organization_id == org)
        )
        assert episode.current_case_id == effect.case_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(DefectCaseRow)
                .where(DefectCaseRow.organization_id == org)
            )
            == 1
        )


def test_downstream_failure_rolls_back_every_effect(runtime, monkeypatch):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, _artifact, task, claim = _running(runtime, now)

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(SqlAlchemyAuditSessionRepository, "append_under_head_lock", fail)
    with pytest.raises(RuntimeError, match="injected"):
        _service(runtime).publish(
            _command(claim, now, ({"defect_type": "scratch", "confidence": 0.91},))
        )
    with runtime() as session:
        assert session.scalar(select(func.count()).select_from(PublishedInferenceResultRow)) == 0
        assert session.scalar(select(func.count()).select_from(InspectionEventRow)) == 0
        assert session.get(InferenceTaskRow, task).status == TaskStatus.RUNNING.value


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_invalid_alert_confidence_rolls_back(runtime, confidence):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, _artifact, task, claim = _running(runtime, now)
    with pytest.raises(ValidationError):
        _service(runtime).publish(
            _command(claim, now, ({"defect_type": "scratch", "confidence": confidence},))
        )
    with runtime() as session:
        assert session.scalar(select(func.count()).select_from(PublishedInferenceResultRow)) == 0
        assert session.get(InferenceTaskRow, task).status == TaskStatus.RUNNING.value
