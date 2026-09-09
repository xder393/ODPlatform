"""Guarded database-owned source-session transitions."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from odp_api.adapters.persistence.inspection_sessions import (
    SqlAlchemyInspectionSessionRepository,
)
from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_models import InspectionSessionRow

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    Base.metadata.create_all(engine)
    yield SqlAlchemyInspectionSessionRepository(sessionmaker(bind=engine), clock=lambda _: NOW)
    engine.dispose()


def _seed(repository, status="START_REQUESTED"):
    session_id, organization_id, camera_id = uuid4(), uuid4(), uuid4()
    with repository._session_factory() as session:
        session.add(
            InspectionSessionRow(
                session_id=session_id,
                organization_id=organization_id,
                camera_id=camera_id,
                line_id=uuid4(),
                source_type="RECORDED",
                sanitized_uri="/safe/fixture.mp4",
                secret_reference=None,
                status=status,
                idempotency_key=str(session_id),
                started_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    return session_id


def test_claim_start_is_atomic_and_heartbeat_is_status_guarded(repository):
    session_id = _seed(repository)

    claimed = repository.claim_start_requests("ingestor-1", 10)
    assert [item.session_id for item in claimed] == [session_id]
    assert claimed[0].status == "RUNNING"
    assert not repository.heartbeat(session_id, "other-ingestor", NOW)
    assert repository.heartbeat(session_id, "ingestor-1", NOW)

    with repository._session_factory() as session:
        row = session.get(InspectionSessionRow, session_id)
        heartbeat = row.heartbeat_at.replace(tzinfo=UTC)
        assert (row.status, heartbeat, row.error_code) == ("RUNNING", NOW, None)

    with repository._session_factory() as session:
        session.get(InspectionSessionRow, session_id).status = "STOP_REQUESTED"
        session.commit()
    assert not repository.heartbeat(session_id, "ingestor-1", NOW)


def test_stop_claim_and_failure_transition_are_durable(repository):
    session_id = _seed(repository)
    repository.claim_start_requests("ingestor-1", 10)
    with repository._session_factory() as session:
        session.get(InspectionSessionRow, session_id).status = "STOP_REQUESTED"
        session.commit()

    stopped = repository.claim_stop_requests("ingestor-1", 10)
    assert [item.session_id for item in stopped] == [session_id]
    assert stopped[0].status == "STOPPED"
    assert not repository.mark_failed(session_id, "ingestor-1", "LATE", "too late", NOW)
