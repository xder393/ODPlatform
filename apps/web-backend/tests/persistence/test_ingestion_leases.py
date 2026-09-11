"""Database-owned ingestion lease transitions."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from odp_api.adapters.persistence.ingestion_ownership import owns_live_session
from odp_api.adapters.persistence.inspection_sessions import (
    SqlAlchemyInspectionSessionRepository,
)
from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.task_models import InspectionSessionRow


@dataclass
class MutableClock:
    current: datetime

    def __call__(self, _session):
        return self.current


NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


@pytest.fixture
def clock():
    return MutableClock(NOW)


@pytest.fixture
def repository(tmp_path, clock):
    engine = create_engine(f"sqlite:///{tmp_path / 'ingestion-leases.db'}")
    Base.metadata.create_all(engine)
    yield SqlAlchemyInspectionSessionRepository(
        sessionmaker(bind=engine), clock=clock
    )
    engine.dispose()


def _seed(
    repository,
    clock,
    *,
    status="START_REQUESTED",
    organization_id=None,
    camera_id=None,
    owner_instance_id=None,
    lease_expires_at=None,
    generation=0,
    last_reserved_sequence=0,
):
    session_id = uuid4()
    organization_id = organization_id or uuid4()
    camera_id = camera_id or uuid4()
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
                ingestor_process_id="legacy-process",
                owner_instance_id=owner_instance_id,
                lease_expires_at=lease_expires_at,
                ingestion_generation=generation,
                last_reserved_sequence=last_reserved_sequence,
                idempotency_key=str(session_id),
                started_at=clock.current,
                created_at=clock.current,
                updated_at=clock.current,
            )
        )
        session.commit()
    return session_id, organization_id, camera_id


def _row(repository, session_id: UUID):
    with repository._session_factory() as session:
        return session.get(InspectionSessionRow, session_id)


def test_claim_renew_and_reclaim_use_strict_lease_boundary(repository, clock):
    _seed(repository, clock)

    first = repository.claim_available("same-name", uuid4(), 1)[0]
    assert first.claim.generation == 1
    assert repository.claim_available("same-name", uuid4(), 1) == []
    clock.current += timedelta(seconds=30)
    assert repository.renew(first.claim) is False
    second = repository.claim_available("same-name", uuid4(), 1)[0]
    assert second.claim.generation == 2
    assert repository.renew(first.claim) is False
    assert repository.renew(second.claim) is True


def test_claim_batch_does_not_revisit_row_after_its_lease_expires(repository, clock):
    session_id, _, _ = _seed(repository, clock)
    advancing_times = iter(
        [NOW + timedelta(seconds=1), NOW + timedelta(seconds=3)]
    )
    batch_repository = SqlAlchemyInspectionSessionRepository(
        repository._session_factory,
        clock=lambda _: next(advancing_times),
        lease_duration=timedelta(seconds=1),
    )

    claimed = batch_repository.claim_available("ingestor", uuid4(), 2)

    assert len(claimed) == 1
    assert claimed[0].session.session_id == session_id
    assert claimed[0].claim.generation == 1


def test_claim_returns_frozen_high_water_and_never_reopens_stop_requested(
    repository, clock
):
    session_id, _, _ = _seed(repository, clock, last_reserved_sequence=17)
    instance_id = uuid4()
    claimed = repository.claim_available("ingestor", instance_id, 1)[0]
    assert claimed.initial_sequence == 17

    with repository._session_factory() as session:
        row = session.get(InspectionSessionRow, session_id)
        row.status = "STOP_REQUESTED"
        session.commit()

    assert repository.claim_available("restarted", uuid4(), 1) == []
    assert repository.stop_candidates(instance_id, 1) == [claimed.claim]


def test_stop_before_claim_and_expired_orphan_stop_are_not_restarted(repository, clock):
    orphan_id, organization_id, camera_id = _seed(
        repository,
        clock,
        status="STOP_REQUESTED",
        owner_instance_id=uuid4(),
        lease_expires_at=NOW - timedelta(seconds=1),
        generation=4,
    )
    assert repository.stop_candidates(uuid4(), 1) == []
    assert repository.finalize_expired_stops(10) == 1
    assert repository.claim_available("recovery", uuid4(), 1) == []
    row = _row(repository, orphan_id)
    assert (row.status, row.owner_instance_id, row.lease_expires_at) == (
        "STOPPED",
        None,
        None,
    )
    assert row.stopped_at.replace(tzinfo=UTC) == NOW
    assert (row.organization_id, row.camera_id) == (organization_id, camera_id)
    assert repository.finalize_expired_stops(10) == 0


def test_never_owned_stop_request_is_finalized_without_a_claim(repository, clock):
    session_id, _, _ = _seed(repository, clock, status="STOP_REQUESTED")

    assert repository.stop_candidates(uuid4(), 1) == []
    assert repository.finalize_expired_stops(1) == 1

    row = _row(repository, session_id)
    assert (row.status, row.owner_instance_id, row.lease_expires_at) == (
        "STOPPED",
        None,
        None,
    )


@pytest.mark.parametrize("field", ["organization_id", "owner_instance_id", "generation"])
def test_renew_rejects_wrong_claim_tokens(repository, clock, field):
    _seed(repository, clock)
    claimed = repository.claim_available("ingestor", uuid4(), 1)[0]
    if field in {"organization_id", "owner_instance_id"}:
        value = uuid4()
    else:
        value = claimed.claim.generation + 1
    wrong = replace(claimed.claim, **{field: value})
    assert repository.renew(wrong) is False
    assert repository.renew(claimed.claim) is True


def test_finish_stop_accepts_matching_owner_after_expiry_but_not_wrong_owner(
    repository, clock
):
    _seed(repository, clock)
    claimed = repository.claim_available("ingestor", uuid4(), 1)[0]
    with repository._session_factory() as session:
        session.get(InspectionSessionRow, claimed.claim.session_id).status = "STOP_REQUESTED"
        session.commit()

    assert repository.finish_stop(replace(claimed.claim, owner_instance_id=uuid4())) is False
    clock.current += timedelta(seconds=31)
    assert repository.finish_stop(claimed.claim) is True
    row = _row(repository, claimed.claim.session_id)
    assert (row.status, row.owner_instance_id, row.lease_expires_at) == (
        "STOPPED",
        None,
        None,
    )


def test_release_only_expires_matching_running_lease(repository, clock):
    _seed(repository, clock)
    claimed = repository.claim_available("ingestor", uuid4(), 1)[0]
    assert repository.release(replace(claimed.claim, generation=99)) is False
    assert repository.release(claimed.claim) is True
    row = _row(repository, claimed.claim.session_id)
    assert row.status == "RUNNING"
    assert row.owner_instance_id == claimed.claim.owner_instance_id
    assert row.lease_expires_at.replace(tzinfo=UTC) == NOW
    assert repository.release(claimed.claim) is False

    with repository._session_factory() as session:
        session.get(InspectionSessionRow, claimed.claim.session_id).status = "STOP_REQUESTED"
        session.commit()
    assert repository.release(claimed.claim) is False


def test_fail_claim_rejects_late_failure_and_accepts_current_owner(repository, clock):
    _seed(repository, clock)
    claimed = repository.claim_available("ingestor", uuid4(), 1)[0]
    clock.current += timedelta(seconds=30)
    assert repository.fail_claim(claimed.claim, "LATE", "stale source") is False

    replacement = repository.claim_available("restarted", uuid4(), 1)[0]
    assert repository.fail_claim(claimed.claim, "LATE", "stale source") is False
    assert repository.fail_claim(replacement.claim, "SOURCE_ERROR", "camera stopped") is True
    row = _row(repository, replacement.claim.session_id)
    assert (row.status, row.error_code, row.error_detail) == (
        "FAILED",
        "SOURCE_ERROR",
        "camera stopped",
    )


def test_live_session_predicate_checks_every_token_and_strict_expiry(repository, clock):
    _seed(repository, clock)
    claimed = repository.claim_available("ingestor", uuid4(), 1)[0]
    row = _row(repository, claimed.claim.session_id)
    assert owns_live_session(row, claimed.claim, NOW)
    assert not owns_live_session(row, claimed.claim, NOW + timedelta(seconds=30))
    assert not owns_live_session(
        row,
        replace(claimed.claim, camera_id=uuid4()),
        NOW,
    )


@pytest.mark.parametrize(
    "lease_duration",
    [0, -1, timedelta(0), timedelta(seconds=-1), float("inf")],
)
def test_invalid_lease_duration_is_rejected(tmp_path, clock, lease_duration):
    engine = create_engine(f"sqlite:///{tmp_path / 'invalid-lease.db'}")
    try:
        with pytest.raises(ValueError, match="lease"):
            SqlAlchemyInspectionSessionRepository(
                sessionmaker(bind=engine), clock=clock, lease_duration=lease_duration
            )
    finally:
        engine.dispose()
