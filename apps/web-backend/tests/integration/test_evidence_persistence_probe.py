"""A persistence observer must reject missing, changed or unrelated evidence."""

import hashlib
import importlib.util
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from odp_api.adapters.persistence.models import (
    Base,
    CaseTransitionRow,
    DefectCaseRow,
    InspectionEventRow,
)
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    PublishedInferenceResultRow,
)

SCRIPT = Path(__file__).parents[2] / "scripts" / "verify_evidence_persistence.py"
PAYLOAD = b"original evidence frame"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
NOW = datetime(2026, 9, 11, tzinfo=UTC)


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("evidence_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ObjectResponse(io.BytesIO):
    def release_conn(self):
        pass


class EvidenceStore:
    # Only external object I/O is substituted; the DB query and digest are real.
    def __init__(self, payload=PAYLOAD):
        self.objects = {("evidence", "frames/original.jpg"): payload}

    def get_object(self, bucket, key):
        return ObjectResponse(self.objects[(bucket, key)])


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'evidence.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    organization, case_id, event_id, result_id, artifact_id = (uuid4() for _ in range(5))
    with sessions.begin() as session:
        session.add(DefectCaseRow(
            case_id=case_id, organization_id=organization, status="RESOLVED", updated_at=NOW,
        ))
        session.add(CaseTransitionRow(
            transition_id=uuid4(), case_id=case_id, organization_id=organization,
            from_status="IN_REVIEW", to_status="RESOLVED", actor_id=uuid4(), occurred_at=NOW,
        ))
        session.add(FrameArtifactRow(
            artifact_id=artifact_id, organization_id=organization, camera_id=uuid4(),
            stream_session_id=uuid4(), frame_sequence=1, ingestion_generation=1,
            captured_at=NOW, object_key="frames/original.jpg", sha256=DIGEST,
            content_length=len(PAYLOAD), state="AVAILABLE", lifecycle="EVIDENCE",
            created_at=NOW, updated_at=NOW,
        ))
        session.add(PublishedInferenceResultRow(
            result_id=result_id, organization_id=organization, task_id=uuid4(), attempt_id=uuid4(),
            artifact_id=artifact_id, model_release="fixture", model_sha256="a" * 64,
            onnxruntime_version="test", execution_provider="CPUExecutionProvider",
            actual_input_shape=[1, 3, 64, 64], preprocessing_version="test",
            postprocessing_version="test", confidence_threshold=0.5, iou_threshold=0.5,
            nms_mode="test", nms_in_model=False, class_map_version="test",
            frame_sha256=DIGEST, detections=[], stage_durations={}, published_at=NOW, created_at=NOW,
        ))
        session.add(InspectionEventRow(
            event_id=event_id, case_id=case_id, organization_id=organization, camera_id=uuid4(),
            occurred_at=NOW, defect_class="scratch", confidence=0.9, model_release="fixture",
            preprocessing_parameters=[], threshold=0.5, input_frame_sha256=DIGEST,
            source_result_id=result_id, evidence_artifact_id=artifact_id,
        ))
    try:
        yield sessions
    finally:
        engine.dispose()


def test_observer_records_and_verifies_original_case_and_bytes(probe, database):
    before = probe.observe(database, EvidenceStore(), "evidence")
    assert before["sha256"] == DIGEST
    assert before["content_length"] == len(PAYLOAD)
    assert before["status"] == "RESOLVED"
    assert before["history"][0]["to_status"] == "RESOLVED"
    assert probe.observe(database, EvidenceStore(), "evidence", expected=before) == before


def test_observer_rejects_corrupted_object_bytes(probe, database):
    with pytest.raises(probe.ProbeError, match="digest"):
        probe.observe(database, EvidenceStore(b"corrupted evidence"), "evidence")


@pytest.mark.parametrize("mutation", ["missing-case", "wrong-result", "wrong-tenant", "history"])
def test_verify_does_not_accept_a_replacement_or_changed_linkage(probe, database, mutation):
    before = probe.observe(database, EvidenceStore(), "evidence")
    with database.begin() as session:
        if mutation == "missing-case":
            session.delete(session.get(DefectCaseRow, UUID(before["case_id"])))
        elif mutation == "wrong-result":
            session.get(PublishedInferenceResultRow, UUID(before["result_id"])).artifact_id = uuid4()
        elif mutation == "wrong-tenant":
            session.get(FrameArtifactRow, UUID(before["artifact_id"])).organization_id = uuid4()
        else:
            session.query(CaseTransitionRow).delete()
    with pytest.raises(probe.ProbeError):
        probe.observe(database, EvidenceStore(), "evidence", expected=before)


def test_prepare_cannot_pass_with_no_real_inference_cases(probe, database):
    with database.begin() as session:
        session.query(InspectionEventRow).update({"source_result_id": None})
    with pytest.raises(probe.ProbeError, match="linked"):
        probe.observe(database, EvidenceStore(), "evidence")


def test_cli_requires_opt_in_and_preserves_existing_state(probe, monkeypatch, tmp_path):
    state = tmp_path / "evidence.json"
    monkeypatch.delenv("ODP_ALLOW_COMPOSE_PROBE", raising=False)
    assert probe.cli(["prepare", "--state", str(state)]) != 0
    assert not state.exists()
    monkeypatch.setenv("ODP_ALLOW_COMPOSE_PROBE", "disposable")
    state.write_text("retained")
    assert probe.cli(["prepare", "--state", str(state)]) != 0
    assert state.read_text() == "retained"


@pytest.mark.parametrize("value", [{}, {"password": "do-not-store"}, {"schema_version": 999}])
def test_cli_rejects_invalid_state_without_database_access(probe, monkeypatch, tmp_path, value):
    monkeypatch.setenv("ODP_ALLOW_COMPOSE_PROBE", "disposable")
    monkeypatch.setenv("ODP_DATABASE_URL", "this-must-not-be-opened")
    state = tmp_path / "evidence.json"
    state.write_text(json.dumps(value))
    def must_not_open_database(*args, **kwargs):
        raise AssertionError("state must be rejected before database access")
    monkeypatch.setattr(probe, "create_engine", must_not_open_database)
    assert probe.cli(["verify", "--state", str(state)]) != 0
    assert json.loads(state.read_text()) == value
