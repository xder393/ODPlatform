"""Read-only evidence persistence acceptance for disposable Compose projects."""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC
from pathlib import Path
from uuid import UUID

from minio import Minio
from minio.error import MinioException
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from urllib3 import PoolManager, Timeout
from urllib3.exceptions import HTTPError

from odp_api.adapters.persistence.models import (
    CaseTransitionRow,
    DefectCaseRow,
    InspectionEventRow,
)
from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    PublishedInferenceResultRow,
)


class ProbeError(RuntimeError):
    pass


def observe(sessions, storage, bucket, *, expected=None):
    """Download evidence reached through a resolved case and its published result.

    The second observation targets the saved IDs, never a newly seeded case.
    This observes DB/S3 durability; browser authorization has its own E2E gate.
    """
    with sessions() as session:
        statement = (
            select(DefectCaseRow, InspectionEventRow, PublishedInferenceResultRow, FrameArtifactRow)
            .join(InspectionEventRow, InspectionEventRow.case_id == DefectCaseRow.case_id)
            .join(PublishedInferenceResultRow,
                  PublishedInferenceResultRow.result_id == InspectionEventRow.source_result_id)
            .join(FrameArtifactRow,
                  FrameArtifactRow.artifact_id == InspectionEventRow.evidence_artifact_id)
            .where(
                DefectCaseRow.status == "RESOLVED",
                InspectionEventRow.organization_id == DefectCaseRow.organization_id,
                PublishedInferenceResultRow.organization_id == DefectCaseRow.organization_id,
                FrameArtifactRow.organization_id == DefectCaseRow.organization_id,
                PublishedInferenceResultRow.artifact_id == FrameArtifactRow.artifact_id,
                FrameArtifactRow.state == "AVAILABLE",
                FrameArtifactRow.lifecycle == "EVIDENCE",
            )
        )
        if expected is not None:
            statement = statement.where(
                DefectCaseRow.case_id == UUID(expected["case_id"]),
                InspectionEventRow.event_id == UUID(expected["event_id"]),
                DefectCaseRow.organization_id == UUID(expected["organization_id"]),
            )
        row = session.execute(statement.order_by(
            DefectCaseRow.updated_at.desc(), InspectionEventRow.event_id,
        ).limit(1)).first()
        if row is None:
            raise ProbeError("No linked resolved case/result/evidence found")
        case, event, result, artifact = row
        if not artifact.object_key or not (
            event.input_frame_sha256 == result.frame_sha256 == artifact.sha256
        ):
            raise ProbeError("Evidence linkage or recorded digest is inconsistent")
        history = session.scalars(select(CaseTransitionRow).where(
            CaseTransitionRow.case_id == case.case_id,
            CaseTransitionRow.organization_id == case.organization_id,
        ).order_by(CaseTransitionRow.occurred_at, CaseTransitionRow.transition_id)).all()
        if not history:
            raise ProbeError("Resolved case has no durable history")
        snapshot = {
            "schema_version": 1,
            "organization_id": str(case.organization_id),
            "case_id": str(case.case_id), "status": case.status,
            "event_id": str(event.event_id), "result_id": str(result.result_id),
            "artifact_id": str(artifact.artifact_id), "bucket": bucket,
            "object_key": artifact.object_key, "sha256": artifact.sha256,
            "content_length": artifact.content_length,
            "history": [{
                "transition_id": str(item.transition_id),
                "from_status": item.from_status, "to_status": item.to_status,
                "actor_id": str(item.actor_id) if item.actor_id else None,
                "correlation_id": str(item.correlation_id) if item.correlation_id else None,
                "occurred_at": (
                    item.occurred_at.replace(tzinfo=UTC) if item.occurred_at.tzinfo is None
                    else item.occurred_at.astimezone(UTC)
                ).isoformat(),
            } for item in history],
        }
    if expected is not None and snapshot != expected:
        raise ProbeError("Original case/history/evidence metadata changed after recreation")
    response = storage.get_object(bucket, snapshot["object_key"])
    digest, size = hashlib.sha256(), 0
    try:
        while chunk := response.read(64 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        response.close()
        response.release_conn()
    if digest.hexdigest() != snapshot["sha256"] or size != snapshot["content_length"]:
        raise ProbeError("Downloaded evidence digest or length differs from the original frame")
    return snapshot


def load_state(path):
    value = json.loads(path.read_text())
    keys = {"schema_version", "organization_id", "case_id", "event_id", "result_id",
            "artifact_id", "status", "bucket", "object_key", "sha256", "content_length", "history"}
    if not isinstance(value, dict) or set(value) != keys or value["schema_version"] != 1:
        raise ProbeError("Invalid evidence state schema")
    for key in ("organization_id", "case_id", "event_id", "result_id", "artifact_id"):
        UUID(value[key])
    if (value["status"] != "RESOLVED" or not isinstance(value["history"], list)
            or not value["history"] or not isinstance(value["content_length"], int)
            or value["content_length"] <= 0 or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
        raise ProbeError("Invalid evidence state values")
    return value


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "verify"))
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        if os.getenv("ODP_ALLOW_COMPOSE_PROBE") != "disposable":
            raise ProbeError("Run only in an explicitly opted-in disposable Compose project")
        if args.phase == "prepare" and args.state.exists():
            raise ProbeError("Refusing to overwrite existing evidence state")
        expected = load_state(args.state) if args.phase == "verify" else None
        engine = create_engine(os.environ["ODP_DATABASE_URL"])
        storage = Minio(
            os.environ["ODP_MINIO_ENDPOINT"],
            access_key=os.environ["ODP_MINIO_ACCESS_KEY"],
            secret_key=os.environ["ODP_MINIO_SECRET_KEY"],
            secure=os.getenv("ODP_MINIO_SECURE", "false").lower() == "true",
            region=os.getenv("ODP_MINIO_REGION", "us-east-1"),
            http_client=PoolManager(timeout=Timeout(connect=5, read=10), retries=False),
        )
        snapshot = observe(sessionmaker(engine), storage, os.environ["ODP_MINIO_BUCKET"],
                           expected=expected)
        if args.phase == "prepare":
            # Exclusive creation: a rerun must not silently replace the baseline.
            with args.state.open("x") as output:
                json.dump(snapshot, output, sort_keys=True)
        print(json.dumps({"status": "PASS", "phase": args.phase,
                          "case_id": snapshot["case_id"], "sha256": snapshot["sha256"]}))
        return 0
    except (ProbeError, OSError, ValueError, KeyError, TypeError,
            SQLAlchemyError, MinioException, HTTPError) as error:
        # SDK/DB errors can embed credentials, URLs and object keys. Only our
        # controlled diagnostic text is printable; retain server logs separately.
        detail = str(error) if isinstance(error, ProbeError) else type(error).__name__
        print(f"Evidence persistence verification failed: {detail}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(cli())
