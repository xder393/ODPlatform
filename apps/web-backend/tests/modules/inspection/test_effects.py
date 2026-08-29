"""Business effects published by the fenced inspection boundary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from odp_api.adapters.persistence.inspection_effects import SqlAlchemyInspectionEffects
from odp_api.adapters.persistence.models import (
    AlertRow,
    AuditLogRow,
    Base,
    DefectCaseRow,
    InspectionAlertFeedRow,
    InspectionEventRow,
)
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.repositories import SqlAlchemyAuditSessionRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    DefectEpisodeRow,
    FrameArtifactRow,
    InferenceTaskRow,
    OutboxEventRow,
    PublishedInferenceResultRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.inspection.effects import InspectionEffectService, PublishConflict
from odp_api.modules.tasks.commands import InferenceExecutionContract, PublishInferenceCommand
from odp_api.modules.tasks.models import TaskStatus
from odp_api.ports.tasks import StaleLease
from sqlalchemy import func, select


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


def _running(sessions, now, *, camera=None, organization=None):
    org, camera, artifact, task = organization or uuid4(), camera or uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add_all(
            (
                FrameArtifactRow(
                    artifact_id=artifact,
                    organization_id=org,
                    camera_id=camera,
                    stream_session_id=uuid4(),
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
    return InspectionEffectService(SqlAlchemyInspectionEffects(sessions))


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
    effect = _service(runtime).publish(
        _command(claim, now, ({"defect_type": "scratch", "confidence": 0.91, "spatial_zone": "A"},))
    )
    assert effect.event_id and effect.case_id and effect.alert_outbox_id
    with runtime() as session:
        assert session.get(FrameArtifactRow, artifact).lifecycle == "EVIDENCE"
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


def test_episode_reuses_open_case_and_expiry_creates_a_new_case(runtime):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    org, camera, _artifact, _task, claim = _running(runtime, now)
    first = _service(runtime).publish(
        _command(claim, now, ({"defect_type": "scratch", "confidence": 0.91},))
    )
    _org2, _camera2, _artifact2, _task2, claim2 = _running(
        runtime, now, camera=camera, organization=org
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


def test_downstream_failure_rolls_back_every_effect(runtime, monkeypatch):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    _org, _camera, _artifact, task, claim = _running(runtime, now)
    def fail(*_args, **_kwargs):
        raise RuntimeError("injected")
    monkeypatch.setattr(SqlAlchemyAuditSessionRepository, "append_under_head_lock", fail)
    with pytest.raises(RuntimeError, match="injected"):
        _service(runtime).publish(_command(claim, now, ({"defect_type": "scratch", "confidence": 0.91},)))
    with runtime() as session:
        assert session.scalar(select(func.count()).select_from(PublishedInferenceResultRow)) == 0
        assert session.scalar(select(func.count()).select_from(InspectionEventRow)) == 0
        assert session.get(InferenceTaskRow, task).status == TaskStatus.RUNNING.value
