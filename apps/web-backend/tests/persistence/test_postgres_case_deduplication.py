"""PostgreSQL races for defect-episode and compatibility-audit primitives."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from os import environ
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import func, select

from alembic import command
from odp_api.adapters.persistence import inspection_effects
from odp_api.adapters.persistence.models import AuditChainHeadRow, AuditLogRow
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    DefectEpisodeRow,
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
)
from odp_api.db import create_engine_and_session

pytestmark = pytest.mark.skipif(
    not environ.get("ODP_POSTGRES_TEST_URL"), reason="ODP_POSTGRES_TEST_URL not configured"
)


def _migrated_database():
    database_url = environ["ODP_POSTGRES_TEST_URL"]
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return create_engine_and_session(database_url)


def test_episode_claim_primitive_serializes_the_missing_row_race():
    engine, sessions = _migrated_database()
    organization_id, camera_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    gate = Barrier(2)

    def claim_episode():
        with sessions.begin() as session:
            gate.wait(timeout=10)
            episode = inspection_effects.claim_defect_episode(
                session,
                organization_id,
                camera_id,
                "scratch",
                "GLOBAL",
                now,
            )
            return episode.episode_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim_episode) for _ in range(2)]
            episode_ids = [future.result(timeout=30) for future in futures]
        assert episode_ids[0] == episode_ids[1]
        with sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(DefectEpisodeRow)
                    .where(
                        DefectEpisodeRow.organization_id == organization_id,
                        DefectEpisodeRow.camera_id == camera_id,
                        DefectEpisodeRow.defect_type == "scratch",
                        DefectEpisodeRow.spatial_zone == "GLOBAL",
                    )
                )
                == 1
            )
    finally:
        engine.dispose()


def test_two_first_compatibility_replays_append_one_canonical_audit_chain():
    engine, sessions = _migrated_database()
    organization_id = uuid4()
    now = datetime.now(UTC)
    task_ids = []
    with sessions.begin() as session:
        for sequence in (1, 2):
            camera_id, stream_session_id, artifact_id, task_id = (
                uuid4(),
                uuid4(),
                uuid4(),
                uuid4(),
            )
            task_ids.append(task_id)
            session.add_all(
                [
                    InspectionSessionRow(
                        session_id=stream_session_id,
                        organization_id=organization_id,
                        camera_id=camera_id,
                        line_id=uuid4(),
                        source_type="TEST",
                        sanitized_uri="rtsp://test.invalid/camera",
                        status="RUNNING",
                        idempotency_key=str(stream_session_id),
                    ),
                    CameraInferenceStateRow(
                        organization_id=organization_id,
                        camera_id=camera_id,
                        ready_count=0,
                        version=0,
                    ),
                    FrameArtifactRow(
                        artifact_id=artifact_id,
                        organization_id=organization_id,
                        camera_id=camera_id,
                        stream_session_id=stream_session_id,
                        frame_sequence=sequence,
                        captured_at=now,
                        sha256=f"{sequence:064x}",
                        state="AVAILABLE",
                        lifecycle="PROCESSING",
                    ),
                    InferenceTaskRow(
                        task_id=task_id,
                        organization_id=organization_id,
                        camera_id=camera_id,
                        artifact_id=artifact_id,
                        idempotency_key=str(task_id),
                        status="BLOCKED_COMPATIBILITY",
                        dispatch_seq=1,
                        attempt_count=0,
                        fence_token=0,
                    ),
                ]
            )
    gate = Barrier(2)

    def replay(task_id):
        gate.wait(timeout=10)
        return SqlAlchemyTaskControlRepository(sessions).replay_compatibility(
            task_id, organization_id, now
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(replay, task_id) for task_id in task_ids]
            [future.result(timeout=30) for future in futures]
        with sessions() as session:
            entries = session.scalars(
                select(AuditLogRow)
                .where(AuditLogRow.organization_id == organization_id)
                .order_by(AuditLogRow.sequence)
            ).all()
            head = session.get(AuditChainHeadRow, organization_id)
            assert [entry.sequence for entry in entries] == [1, 2]
            assert entries[0].previous_hash == "0" * 64
            assert entries[1].previous_hash == entries[0].entry_hash
            assert head.last_sequence == 2
            assert head.head_hash == entries[1].entry_hash
    finally:
        engine.dispose()
