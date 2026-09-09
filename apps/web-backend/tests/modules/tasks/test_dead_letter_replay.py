"""Durable supervisor replay of terminal inference tasks."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from odp_api.adapters.persistence.models import AuditLogRow, Base
from odp_api.adapters.persistence.repositories import (
    SqlAlchemyAuditRepository,
)
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    OutboxEventRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.audit.service import AuditService
from odp_api.modules.tasks.models import TaskStatus
from odp_api.ports.tasks import AdmissionRejected


def test_dead_letter_replay_is_new_task_and_audited_without_mutating_history(tmp_path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'replay.db'}")
    Base.metadata.create_all(engine)
    organization_id, camera_id = uuid4(), uuid4()
    session_id, artifact_id, source_task_id = uuid4(), uuid4(), uuid4()
    actor_id = uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    with sessions.begin() as session:
        session.add_all(
            [
                InspectionSessionRow(
                    session_id=session_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    line_id=uuid4(),
                    source_type="RECORDED",
                    sanitized_uri="/fixtures/frame.mp4",
                    secret_reference=None,
                    status="STOPPED",
                    idempotency_key=str(session_id),
                    started_at=now,
                    stopped_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                FrameArtifactRow(
                    artifact_id=artifact_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    stream_session_id=session_id,
                    frame_sequence=1,
                    captured_at=now,
                    object_key="frames/original.jpg",
                    sha256="a" * 64,
                    content_length=10,
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    created_at=now,
                    updated_at=now,
                ),
                InferenceTaskRow(
                    task_id=source_task_id,
                    organization_id=organization_id,
                    camera_id=camera_id,
                    artifact_id=artifact_id,
                    idempotency_key="source-task",
                    status=TaskStatus.DEAD_LETTER.value,
                    dispatch_seq=1,
                    attempt_count=3,
                    fence_token=3,
                    error_code="INVALID_INPUT",
                    error_detail="fixture failure",
                    created_at=now,
                    updated_at=now,
                ),
                CameraInferenceStateRow(
                    organization_id=organization_id,
                    camera_id=camera_id,
                    ready_count=0,
                    version=3,
                    updated_at=now,
                ),
            ]
        )

    repository = SqlAlchemyTaskControlRepository(sessions, clock=lambda _: now)
    replay = repository.replay_dead_letter(source_task_id, organization_id, actor_id)

    with sessions() as session:
        source = session.get(InferenceTaskRow, source_task_id)
        replacement = session.get(InferenceTaskRow, replay.task_id)
        outbox = session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.outbox_id == replay.new_outbox_id)
        )
        state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
        audit = session.scalars(
            select(AuditLogRow).where(AuditLogRow.organization_id == organization_id)
        ).all()

        assert source is not None and source.status == TaskStatus.DEAD_LETTER.value
        assert replacement is not None
        assert replacement.task_id != source_task_id
        assert replacement.status == TaskStatus.READY.value
        assert replacement.artifact_id == artifact_id
        assert replacement.dispatch_seq == replay.dispatch_seq == 1
        assert outbox is not None and outbox.task_id == replacement.task_id
        assert outbox.dispatch_seq == 1
        assert state is not None and state.ready_count == 1
        assert len(audit) == 1 and audit[0].action == "DEAD_LETTER_REPLAYED"
        assert audit[0].actor_id == actor_id

    assert AuditService(SqlAlchemyAuditRepository(sessions)).verify_organization_chain(
        organization_id
    ).is_valid

    second = repository.replay_dead_letter(source_task_id, organization_id, actor_id)
    assert second.task_id != replay.task_id
    with sessions() as session:
        state = session.get(CameraInferenceStateRow, (organization_id, camera_id))
        assert state is not None and state.ready_count == 2

    with pytest.raises(AdmissionRejected, match="READY_WINDOW_FULL"):
        repository.replay_dead_letter(source_task_id, organization_id, actor_id)

    engine.dispose()
