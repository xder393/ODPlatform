"""Tests for the disposable crash-recovery observer and its state contract."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InferenceAttemptRow,
    InferenceTaskRow,
    InspectionSessionRow,
    PublishedInferenceResultRow,
)

SCRIPT = Path(__file__).parents[2] / "scripts" / "verify_ingestor_recovery.py"
spec = importlib.util.spec_from_file_location("verify_ingestor_recovery", SCRIPT)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'probe.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        engine.dispose()


def _seed_committed_result(sessions, *, sequence: int = 7, generation: int = 2):
    organization_id, camera_id, session_id = uuid4(), uuid4(), uuid4()
    artifact_id, task_id, attempt_id, result_id = (uuid4() for _ in range(4))
    with sessions.begin() as session:
        session.add(
            InspectionSessionRow(
                session_id=session_id,
                organization_id=organization_id,
                camera_id=camera_id,
                line_id=uuid4(),
                source_type="RECORDED",
                sanitized_uri="/workspace/.recovery-video.avi",
                status="RUNNING",
                ingestor_process_id="frame-ingestor",
                owner_instance_id=uuid4(),
                ingestion_generation=generation,
                last_reserved_sequence=sequence,
                idempotency_key=str(session_id),
                started_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            FrameArtifactRow(
                artifact_id=artifact_id,
                organization_id=organization_id,
                camera_id=camera_id,
                stream_session_id=session_id,
                frame_sequence=sequence,
                ingestion_generation=generation,
                captured_at=NOW,
                object_key=f"frames/{artifact_id}.jpg",
                sha256="a" * 64,
                content_length=1,
                state="AVAILABLE",
                lifecycle="EVIDENCE",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            InferenceTaskRow(
                task_id=task_id,
                organization_id=organization_id,
                camera_id=camera_id,
                artifact_id=artifact_id,
                idempotency_key=f"task-{task_id}",
                status="SUCCEEDED",
                dispatch_seq=1,
                attempt_count=1,
                fence_token=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            InferenceAttemptRow(
                attempt_id=attempt_id,
                task_id=task_id,
                organization_id=organization_id,
                worker_id="inference-worker-1",
                attempt_no=1,
                fence_token=1,
                started_at=NOW,
                finished_at=NOW,
                outcome="SUCCEEDED",
                created_at=NOW,
            )
        )
        session.add(
            PublishedInferenceResultRow(
                result_id=result_id,
                organization_id=organization_id,
                task_id=task_id,
                attempt_id=attempt_id,
                artifact_id=artifact_id,
                model_release="test-model",
                model_sha256="b" * 64,
                onnxruntime_version="test",
                execution_provider="CPUExecutionProvider",
                actual_input_shape=[1, 3, 64, 64],
                preprocessing_version="test",
                postprocessing_version="test",
                confidence_threshold=0.5,
                iou_threshold=0.5,
                nms_mode="test",
                nms_in_model=False,
                class_map_version="test",
                frame_sha256="a" * 64,
                detections=[],
                stage_durations={},
                published_at=NOW,
                created_at=NOW,
            )
        )
    return probe.ProbeState(
        organization_id=organization_id,
        session_id=session_id,
        first_generation=generation,
        highest_committed_sequence=sequence,
    )


def test_observer_reports_durable_result_and_all_linkages(database):
    state = _seed_committed_result(database)

    observation = probe.observe_database(database, state)

    assert observation.session_id == state.session_id
    assert observation.organization_id == state.organization_id
    assert observation.status == "RUNNING"
    assert observation.generation == state.first_generation
    assert observation.artifact_generation == state.first_generation
    assert observation.highest_committed_sequence == state.highest_committed_sequence
    assert observation.result_id is not None
    assert observation.task_id is not None
    assert observation.artifact_id is not None
    assert observation.task_status == "SUCCEEDED"
    assert observation.artifact_state == "AVAILABLE"
    assert observation.artifact_lifecycle == "EVIDENCE"


def test_recovery_acceptance_requires_both_generation_and_sequence_advance(database):
    state = _seed_committed_result(database)
    observation = probe.observe_database(database, state)

    with pytest.raises(probe.ProbeError, match="generation and sequence"):
        probe.assert_recovery_advanced(state, observation)

    assert probe.is_recovery_advanced(
        state,
        replace(
            observation,
            generation=state.first_generation + 1,
            artifact_generation=state.first_generation + 1,
            highest_committed_sequence=state.highest_committed_sequence + 1,
        ),
    )


def test_recovery_rejects_higher_sequence_from_previous_generation(database):
    state = _seed_committed_result(database)
    with database.begin() as session:
        source = session.scalar(
            select(InspectionSessionRow).where(
                InspectionSessionRow.session_id == state.session_id
            )
        )
        artifact = session.scalar(
            select(FrameArtifactRow).where(
                FrameArtifactRow.stream_session_id == state.session_id
            )
        )
        assert source is not None and artifact is not None
        source.ingestion_generation = state.first_generation + 1
        source.last_reserved_sequence = state.highest_committed_sequence + 1
        artifact.frame_sequence = state.highest_committed_sequence + 1

    late_old_generation = probe.observe_database(database, state)
    assert late_old_generation.generation == state.first_generation + 1
    assert late_old_generation.artifact_generation == state.first_generation
    assert late_old_generation.highest_committed_sequence == (
        state.highest_committed_sequence + 1
    )
    with pytest.raises(probe.ProbeError, match="generation and sequence"):
        probe.assert_recovery_advanced(state, late_old_generation)

    with database.begin() as session:
        artifact = session.scalar(
            select(FrameArtifactRow).where(
                FrameArtifactRow.stream_session_id == state.session_id
            )
        )
        assert artifact is not None
        artifact.ingestion_generation = state.first_generation + 1

    recovered_generation = probe.observe_database(database, state)
    assert recovered_generation.highest_committed_sequence == (
        state.highest_committed_sequence + 1
    )
    probe.assert_recovery_advanced(state, recovered_generation)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_probe_rejects_nonfinite_poll_and_timeout_bounds(monkeypatch, raw):
    monkeypatch.setenv("ODP_PROBE_POLL_SECONDS", raw)
    with pytest.raises(probe.ProbeError, match="positive number"):
        probe._poll_seconds()

    monkeypatch.setenv("ODP_TEST_TIMEOUT_SECONDS", raw)
    with pytest.raises(probe.ProbeError, match="positive number"):
        probe._timeout("ODP_TEST_TIMEOUT_SECONDS", 1.0)


def test_state_validation_rejects_credentials_and_wrong_types(monkeypatch, tmp_path: Path):
    # The CLI normally runs inside /workspace; the test opts into its isolated
    # temporary workspace explicitly so path validation remains exercised.
    # This environment variable is only read by the probe state loader.
    # (The test process itself never contacts a compose service.)
    monkeypatch.setenv("ODP_PROBE_WORKSPACE", str(tmp_path))
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "organization_id": str(uuid4()),
                "session_id": str(uuid4()),
                "first_generation": "1",
                "highest_committed_sequence": 1,
                "password": "must never be persisted",
            }
        )
    )

    with pytest.raises(probe.ProbeError, match="exactly"):
        probe.load_state(path)


def test_cli_refuses_missing_opt_in_before_state_or_database_mutation(
    monkeypatch, tmp_path: Path
):
    monkeypatch.delenv("ODP_ALLOW_COMPOSE_PROBE", raising=False)
    monkeypatch.setenv("ODP_PROBE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ODP_DATABASE_URL", "sqlite:////should-not-open.db")
    state_path = tmp_path / "state.json"

    assert probe.cli(["prepare", "--state", str(state_path)]) != 0
    assert not state_path.exists()


def test_cli_rejects_malformed_state_before_opening_database(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ODP_ALLOW_COMPOSE_PROBE", "disposable")
    monkeypatch.setenv("ODP_PROBE_WORKSPACE", str(tmp_path))
    state_path = tmp_path / "state.json"
    state_path.write_text("{not-json")

    def fail_if_opened():
        raise AssertionError("database must not be opened for malformed state")

    monkeypatch.setattr(probe, "build_session_factory", fail_if_opened)

    assert probe.cli(["verify", "--state", str(state_path)]) != 0
