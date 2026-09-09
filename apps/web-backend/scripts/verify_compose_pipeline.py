"""Run inside the frame-ingestor container of a disposable Compose project.

Only inserts a session and observes durable effects. Never calls an ingestor,
relay or Worker method. Retains uniquely scoped DB/evidence facts for inspection.
The synthetic ONNX fixture proves wiring, not detection accuracy.
"""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
from minio import Minio
from redis import Redis
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from odp_api.adapters.persistence.models import (
    AlertRow,
    AuditLogRow,
    DefectCaseRow,
    InspectionEventRow,
)
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    PublishedInferenceResultRow,
)


def main():
    if os.getenv("ODP_ALLOW_COMPOSE_PROBE") != "disposable":
        raise RuntimeError("Run only in a disposable Compose project; opt in explicitly")
    engine = create_engine(os.environ["ODP_DATABASE_URL"])
    sessions = sessionmaker(engine)
    redis = Redis.from_url(os.environ["ODP_REDIS_URL"])
    organization, camera, session_id = uuid4(), uuid4(), uuid4()
    deadline = time.monotonic() + 45
    presence = "odp:inference:worker-presence:" + os.environ["ODP_MODEL_SHA256"]
    while not redis.get(presence):
        if time.monotonic() >= deadline:
            raise AssertionError("No independently running Worker heartbeat")
        time.sleep(0.25)
    with tempfile.TemporaryDirectory(prefix="odp-compose-probe-") as directory:
        video = Path(directory) / "surface.avi"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 5, (64, 64))
        assert writer.isOpened()
        try:
            frame = np.zeros((64, 64, 3), dtype=np.uint8)
            cv2.line(frame, (5, 5), (55, 55), (255, 255, 255), 2)
            writer.write(frame)
        finally:
            writer.release()
        with sessions.begin() as session:
            session.add(InspectionSessionRow(
                session_id=session_id, organization_id=organization, camera_id=camera,
                line_id=uuid4(), source_type="RECORDED", sanitized_uri=str(video),
                status="START_REQUESTED", idempotency_key=str(session_id),
            ))
        print(json.dumps({"session_id": str(session_id), "organization_id": str(organization)}), flush=True)
        deadline = time.monotonic() + 45
        snapshot = {}
        try:
            while time.monotonic() < deadline:
                with sessions() as session:
                    source = session.get(InspectionSessionRow, session_id)
                    tasks = session.scalars(select(InferenceTaskRow).where(
                        InferenceTaskRow.organization_id == organization)).all()
                    snapshot = {"session": source.status, "error": source.error_detail,
                                "tasks": [task.status for task in tasks]}
                    if source.status == "FAILED":
                        raise AssertionError(snapshot)
                    if any(task.status == "SUCCEEDED" for task in tasks):
                        break
                time.sleep(0.25)
            else:
                raise AssertionError(snapshot)
        finally:
            with sessions.begin() as session:
                session.execute(update(InspectionSessionRow).where(
                    InspectionSessionRow.session_id == session_id,
                    InspectionSessionRow.status.in_(["RUNNING", "START_REQUESTED"]),
                ).values(status="STOP_REQUESTED"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with sessions() as session:
                if session.get(InspectionSessionRow, session_id).status == "STOPPED":
                    break
            time.sleep(0.25)
        else:
            raise AssertionError("Ingestor did not acknowledge session stop")

    # New pool: the result must be persisted, not an identity-map artifact.
    engine.dispose()
    with sessions() as session:
        result = session.scalar(select(PublishedInferenceResultRow).where(
            PublishedInferenceResultRow.organization_id == organization))
        assert result is not None
        artifact = session.get(FrameArtifactRow, result.artifact_id)
        event = session.scalars(select(InspectionEventRow).where(
            InspectionEventRow.source_result_id == result.result_id)).one()
        case = session.get(DefectCaseRow, event.case_id)
        alert = session.scalars(select(AlertRow).where(AlertRow.event_id == event.event_id)).one()
        audit = session.scalars(select(AuditLogRow).where(
            AuditLogRow.organization_id == organization,
            AuditLogRow.resource_id == event.event_id,
            AuditLogRow.action == "INSPECTION_PUBLISHED")).one()
        assert event.case_id == case.case_id
        assert event.source_result_id == result.result_id
        assert event.evidence_artifact_id == artifact.artifact_id
        assert alert.event_id == event.event_id
        assert audit.entry_hash and audit.organization_id == organization
        assert artifact.lifecycle == "EVIDENCE"
        assert result.model_sha256 == os.environ["ODP_MODEL_SHA256"]
        assert result.frame_sha256 == artifact.sha256
        storage = Minio(os.environ["ODP_MINIO_ENDPOINT"],
                        access_key=os.environ["ODP_MINIO_ACCESS_KEY"],
                        secret_key=os.environ["ODP_MINIO_SECRET_KEY"], secure=False)
        response = storage.get_object(os.environ["ODP_MINIO_BUCKET"], artifact.object_key)
        try:
            assert hashlib.sha256(response.read()).hexdigest() == artifact.sha256
        finally:
            response.close()
            response.release_conn()
        cursor, delivered = "0-0", False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not delivered:
            for _, entries in redis.xread({"odp:inspection:alerts": cursor}, count=100, block=500):
                for message_id, fields in entries:
                    cursor = message_id
                    envelope = json.loads(fields[b"envelope"])
                    if envelope["payload"].get("alert_id") == str(alert.alert_id):
                        assert envelope["organization_id"] == str(organization)
                        delivered = True
        assert delivered, "Alert persisted but independent relay did not publish it"
        print(json.dumps({"status": "PASS", "case_id": str(case.case_id),
                          "result_id": str(result.result_id), "alert_id": str(alert.alert_id),
                          "evidence_sha256": artifact.sha256}), flush=True)
    redis.close()
    engine.dispose()


if __name__ == "__main__":
    main()
