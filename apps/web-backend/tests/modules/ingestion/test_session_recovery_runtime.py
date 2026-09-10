"""Runtime contracts for resumable ingestion sessions."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from odp_api.ports.inspection_sessions import (
    ClaimedInspectionSession,
    IngestionClaim,
    InspectionSession,
)
from odp_api.processes import runtime
from odp_api.settings import IngestorSettings


def _claimed(*, initial_sequence: int = 9) -> ClaimedInspectionSession:
    session = InspectionSession(
        session_id=uuid4(),
        organization_id=uuid4(),
        camera_id=uuid4(),
        line_id=uuid4(),
        source_type="RECORDED",
        sanitized_uri="/safe/fixture.mp4",
        secret_reference=None,
        status="RUNNING",
    )
    claim = IngestionClaim(
        session.organization_id,
        session.camera_id,
        session.session_id,
        uuid4(),
        2,
    )
    return ClaimedInspectionSession(session, claim, initial_sequence)


def test_ingestor_settings_use_dedicated_recovery_intervals():
    settings = IngestorSettings.valid_test_instance()

    assert settings.ingestion_heartbeat_seconds == 5
    assert settings.ingestion_lease_seconds == 30
    assert settings.ingestion_poll_seconds == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ingestion_heartbeat_seconds", 0),
        ("ingestion_heartbeat_seconds", float("nan")),
        ("ingestion_poll_seconds", float("inf")),
        ("ingestion_lease_seconds", True),
        ("ingestion_poll_seconds", "not-a-number"),
    ],
)
def test_ingestor_settings_reject_invalid_recovery_intervals(field, value):
    with pytest.raises(ValidationError):
        IngestorSettings(**{field: value})


def test_ingestor_settings_require_three_heartbeat_intervals_per_lease():
    with pytest.raises(ValidationError, match="three heartbeat"):
        IngestorSettings(
            ingestion_heartbeat_seconds=5,
            ingestion_lease_seconds=14,
        )


def test_claimed_source_factory_resumes_from_persisted_sequence():
    claimed = _claimed(initial_sequence=9)

    source = runtime.source_from_session(claimed)

    assert source._sequence == 9


def test_source_factory_keeps_unknown_source_types_rejected():
    session = SimpleNamespace(
        source_type="USB",
        sanitized_uri="/dev/video0",
        camera_id=uuid4(),
        session_id=uuid4(),
    )

    with pytest.raises(ValueError, match="unsupported inspection source type"):
        runtime.source_from_session(session)
