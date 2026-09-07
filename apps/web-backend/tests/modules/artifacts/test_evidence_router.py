"""Evidence URL authorization and lifecycle tests."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from odp_api.adapters.persistence.models import Base, InspectionEventRow
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InspectionSessionRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.artifacts.router import EvidenceService, create_artifacts_router
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.tasks.models import ArtifactLifecycle, ArtifactState

ORG_ID = UUID("10000000-0000-4000-8000-000000000001")
LINE_ID = UUID("20000000-0000-4000-8000-000000000001")


class FakeStorage:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def presign_get(self, object_key: str, expires_seconds: int = 60) -> str:
        self.calls.append((object_key, expires_seconds))
        return f"https://minio.test/{object_key}?expires={expires_seconds}"


def _app(tmp_path: Path, storage: FakeStorage, line_id: UUID = LINE_ID):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'evidence.db'}")
    Base.metadata.create_all(engine)
    session_id, artifact_id, event_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            InspectionSessionRow(
                session_id=session_id,
                organization_id=ORG_ID,
                camera_id=uuid4(),
                line_id=line_id,
                source_type="RECORDED",
                sanitized_uri="fixture.mp4",
                secret_reference=None,
                status="RUNNING",
                ingestor_process_id="test",
                idempotency_key=str(session_id),
                heartbeat_at=now,
                error_code=None,
                error_detail=None,
                started_at=now,
                stopped_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            FrameArtifactRow(
                artifact_id=artifact_id,
                organization_id=ORG_ID,
                camera_id=uuid4(),
                stream_session_id=session_id,
                frame_sequence=1,
                captured_at=now,
                object_key="organizations/org/artifacts/evidence",
                sha256="a" * 64,
                content_length=10,
                state=ArtifactState.AVAILABLE.value,
                lifecycle=ArtifactLifecycle.EVIDENCE.value,
                retention_until=now + timedelta(days=1),
                error_code=None,
                error_detail=None,
                created_at=now,
                updated_at=now,
            )
        )
        artifact = session.scalar(select(FrameArtifactRow).where(FrameArtifactRow.artifact_id == artifact_id))
        session.add(
            InspectionEventRow(
                event_id=event_id,
                case_id=uuid4(),
                organization_id=ORG_ID,
                camera_id=artifact.camera_id,
                occurred_at=now,
                defect_class="scratch",
                confidence=0.9,
                model_release="fixture",
                preprocessing_parameters=[],
                threshold=0.8,
                input_frame_sha256="a" * 64,
                line_id=line_id,
                source_result_id=None,
                evidence_artifact_id=artifact_id,
            )
        )
    app = FastAPI()
    actor = Actor(uuid4(), ORG_ID, Role.INSPECTOR, frozenset({LINE_ID}))
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.include_router(create_artifacts_router(EvidenceService(sessions, storage)))
    return app, engine, artifact_id


def test_evidence_url_requires_available_evidence_and_uses_60_second_ttl(tmp_path: Path) -> None:
    storage = FakeStorage()
    app, engine, artifact_id = _app(tmp_path, storage)
    try:
        response = TestClient(app).get(f"/api/v1/artifacts/{artifact_id}/evidence-url")
        assert response.status_code == 200
        assert response.json()["expires_in"] == 60
        assert storage.calls == [("organizations/org/artifacts/evidence", 60)]
    finally:
        engine.dispose()


def test_evidence_url_returns_404_for_out_of_scope_line(tmp_path: Path) -> None:
    storage = FakeStorage()
    app, engine, artifact_id = _app(tmp_path, storage, line_id=uuid4())
    try:
        response = TestClient(app).get(f"/api/v1/artifacts/{artifact_id}/evidence-url")
        assert response.status_code == 404
        assert storage.calls == []
    finally:
        engine.dispose()
