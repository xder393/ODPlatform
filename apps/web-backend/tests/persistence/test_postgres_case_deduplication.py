"""Concurrent effect publication on PostgreSQL (integration-gated)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from os import environ
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from odp_api.adapters.persistence.inspection_effects import SqlAlchemyInspectionEffects
from odp_api.adapters.persistence.models import Base, DefectCaseRow, InspectionEventRow
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    DefectEpisodeRow,
    FrameArtifactRow,
    InferenceTaskRow,
    OutboxEventRow,
    PublishedInferenceResultRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.inspection.effects import InspectionEffectService
from odp_api.modules.tasks.commands import (
    InferenceExecutionContract,
    PublishInferenceCommand,
)

pytestmark = pytest.mark.skipif(
    not environ.get("ODP_POSTGRES_TEST_URL"), reason="ODP_POSTGRES_TEST_URL not configured"
)


def test_two_independent_claims_share_one_active_case():
    engine, sessions = create_engine_and_session(environ["ODP_POSTGRES_TEST_URL"])
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    org, camera = uuid4(), uuid4()
    with sessions.begin() as s:
        s.add(
            CameraInferenceStateRow(
                organization_id=org,
                camera_id=camera,
                running_task_id=None,
                ready_count=2,
                version=0,
            )
        )
        for _ in range(2):
            artifact, task = uuid4(), uuid4()
            s.add(
                FrameArtifactRow(
                    artifact_id=artifact,
                    organization_id=org,
                    camera_id=camera,
                    stream_session_id=uuid4(),
                    frame_sequence=uuid4().int % 100000 + 1,
                    captured_at=now,
                    sha256="a" * 64,
                    state="AVAILABLE",
                    lifecycle="PROCESSING",
                    created_at=now,
                    updated_at=now,
                )
            )
            s.add(
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
                )
            )
    repo = SqlAlchemyTaskControlRepository(sessions)
    claims = []
    with sessions() as s:
        ids = list(
            s.scalars(
                select(InferenceTaskRow.task_id).where(InferenceTaskRow.organization_id == org)
            )
        )
    for task in ids:
        claim = repo.claim(task, org, "worker-" + str(task), now)
        assert claim
        claims.append(claim)
    gate = Barrier(2)

    def run(claim):
        gate.wait(timeout=10)
        adapter = SqlAlchemyInspectionEffects(sessions)
        cmd = PublishInferenceCommand(
            claim,
            InferenceExecutionContract(
                "m", "b" * 64, "1", "cpu", (1,), "pre", "post", 0.5, 0.5, "hard", False, "c"
            ),
            "a" * 64,
            ({"defect_type": "scratch", "spatial_zone": "GLOBAL", "confidence": 0.9},),
            (),
            uuid4(),
            now + timedelta(seconds=1),
        )
        return InspectionEffectService(adapter).publish(cmd)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            [f.result(timeout=30) for f in [pool.submit(run, c) for c in claims]]
        with sessions() as s:
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(PublishedInferenceResultRow)
                    .where(PublishedInferenceResultRow.organization_id == org)
                )
                == 2
            )
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(InspectionEventRow)
                    .where(InspectionEventRow.organization_id == org)
                )
                == 2
            )
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(DefectCaseRow)
                    .where(DefectCaseRow.organization_id == org)
                )
                == 1
            )
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(DefectEpisodeRow)
                    .where(DefectEpisodeRow.organization_id == org)
                )
                == 1
            )
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(
                        OutboxEventRow.organization_id == org,
                        OutboxEventRow.event_type == "inspection.alert.v1",
                    )
                )
                == 2
            )
    finally:
        engine.dispose()
