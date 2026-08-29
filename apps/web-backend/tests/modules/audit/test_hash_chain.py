import asyncio
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest

WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC)]

from fastapi import FastAPI
from fastapi.testclient import TestClient

from odp_api.main import create_app
from odp_api.modules.audit import verify as audit_verify
from odp_api.modules.audit.models import AuditCommand
from odp_api.modules.audit.service import AuditService, InMemoryAuditRepository
from odp_api.modules.audit.verify import (
    AuditAppendBlocked,
    AuditVerificationMonitor,
    InMemoryP0FailureReporter,
)
from odp_api.modules.cases.errors import InvalidCaseTransition
from odp_api.modules.cases.router import InMemoryCaseRepository, create_cases_router
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.policies import AuthorizationDenied
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.inspection.models import DefectCase, InspectionEvent

ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
RESOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000003")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000004")
OCCURRED_AT = datetime(2026, 8, 19, 12, 30, 45, 123456, tzinfo=UTC)


def command(*, action: str = "case.transition") -> AuditCommand:
    return AuditCommand(
        organization_id=ORGANIZATION_ID,
        resource_type="defect_case",
        resource_id=RESOURCE_ID,
        action=action,
        change_summary='{"from":"PENDING_CONFIRMATION","to":"IN_REVIEW"}',
        actor_id=ACTOR_ID,
        occurred_at=OCCURRED_AT,
        correlation_id=CORRELATION_ID,
        request_ip="203.0.113.20",
    )


def test_hash_uses_the_canonical_sha256_input_for_exact_command_fields() -> None:
    """Changing serialization details must change this independently calculated digest."""
    service = AuditService(InMemoryAuditRepository())

    entry = service.append(command())

    assert entry.previous_hash == "0" * 64
    assert (
        entry.entry_hash
        == "dc39d1bb627353f5dbfaf9e15dab89f3931cc99438da563b06f89f5b239a5316"
    )


def test_concurrent_appends_form_one_continuous_organization_chain() -> None:
    service = AuditService(InMemoryAuditRepository())

    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = list(
            executor.map(
                lambda index: service.append(
                    command(action=f"case.transition.{index}")
                ),
                range(20),
            )
        )

    ordered = sorted(entries, key=lambda entry: entry.sequence)
    assert [entry.sequence for entry in ordered] == list(range(1, 21))
    assert all(
        current.previous_hash == previous.entry_hash
        for previous, current in pairwise(ordered)
    )
    assert service.verify_organization_chain(ORGANIZATION_ID).is_valid


def test_verification_fails_when_a_stored_row_is_tampered() -> None:
    repository = InMemoryAuditRepository()
    service = AuditService(repository)
    first = service.append(command())
    service.append(command(action="case.resolve"))
    repository.unsafe_replace_change_summary_for_test(first.audit_id, "tampered")

    result = service.verify_organization_chain(ORGANIZATION_ID)

    assert not result.is_valid
    assert result.failed_sequence == first.sequence
    assert "entry hash" in result.reason


def test_failed_startup_verification_emits_p0_and_blocks_appends_until_recovery() -> (
    None
):
    repository = InMemoryAuditRepository()
    service = AuditService(repository)
    first = service.append(command())
    repository.unsafe_replace_change_summary_for_test(first.audit_id, "tampered")
    reporter = InMemoryP0FailureReporter()
    monitor = AuditVerificationMonitor(service, reporter)

    results = monitor.startup_sample_verify()

    assert not results[ORGANIZATION_ID].is_valid
    assert reporter.failures[0].organization_id == ORGANIZATION_ID
    with pytest.raises(AuditAppendBlocked):
        service.append(command(action="case.resolve"))

    administrator = Actor(ACTOR_ID, ORGANIZATION_ID, Role.ADMINISTRATOR, frozenset())
    with pytest.raises(audit_verify.AuditRecoveryRejected):
        service.explicit_administrator_recovery(ORGANIZATION_ID, administrator)

    repository.unsafe_replace_change_summary_for_test(
        first.audit_id, '{"from":"PENDING_CONFIRMATION","to":"IN_REVIEW"}'
    )
    service.explicit_administrator_recovery(ORGANIZATION_ID, administrator)
    assert service.append(command(action="case.resolve")).sequence == 2


def test_verification_reads_entries_and_chain_head_from_one_locked_snapshot() -> None:
    repository = InMemoryAuditRepository()
    service = AuditService(repository)
    service.append(command())
    snapshot_started = Event()
    release_snapshot = Event()
    repository.pause_consistent_snapshot_for_test(snapshot_started, release_snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        verification = executor.submit(
            service.verify_organization_chain, ORGANIZATION_ID
        )
        assert snapshot_started.wait(timeout=1)
        append = executor.submit(service.append, command(action="case.resolve"))
        assert not append.done()
        release_snapshot.set()
        assert verification.result(timeout=1).is_valid
        append.result(timeout=1)

    assert service.verify_organization_chain(ORGANIZATION_ID).is_valid


def test_recovery_requires_an_administrator_in_the_affected_organization() -> None:
    service = AuditService(InMemoryAuditRepository())
    service.append(command())
    service.block_appends(ORGANIZATION_ID)
    non_administrator = Actor(ACTOR_ID, ORGANIZATION_ID, Role.SUPERVISOR, frozenset())

    with pytest.raises(AuthorizationDenied):
        service.explicit_administrator_recovery(ORGANIZATION_ID, non_administrator)

    with pytest.raises(AuditAppendBlocked):
        service.append(command(action="case.resolve"))


def test_managed_daily_verifier_runs_and_blocks_a_failed_organization() -> None:
    repository = InMemoryAuditRepository()
    service = AuditService(repository)
    entry = service.append(command())
    repository.unsafe_replace_change_summary_for_test(entry.audit_id, "tampered")
    reporter = InMemoryP0FailureReporter()
    monitor = AuditVerificationMonitor(service, reporter)
    managed = audit_verify.ManagedDailyAuditVerification(
        monitor, interval_seconds=0.001
    )

    async def run_daily_check() -> None:
        managed.start()
        for _ in range(100):
            if reporter.failures:
                break
            await asyncio.sleep(0.001)
        await managed.stop()

    asyncio.run(run_daily_check())

    assert reporter.failures[0].organization_id == ORGANIZATION_ID
    with pytest.raises(AuditAppendBlocked):
        service.append(command(action="case.resolve"))


def test_app_lifespan_starts_and_stops_the_managed_daily_full_verifier() -> None:
    app = create_app(
        daily_verification_interval_seconds=0.001,
        audit_repository=InMemoryAuditRepository(),
    )
    service = app.state.audit_service
    repository = service.repository

    with TestClient(app):
        assert app.state.daily_audit_verification.is_running
        entry = service.append(command())
        repository.unsafe_replace_change_summary_for_test(entry.audit_id, "tampered")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                service.append(command(action="case.resolve"))
            except AuditAppendBlocked:
                break
            time.sleep(0.001)
        else:
            pytest.fail(
                "The managed daily verification did not block the failed organization."
            )

    assert not app.state.daily_audit_verification.is_running


def test_case_transition_and_audit_append_share_a_mutation_boundary() -> None:
    """An audit failure must leave the business mutation uncommitted."""
    repository = InMemoryAuditRepository()
    audit_service = AuditService(repository)
    case = DefectCase(
        case_id=RESOURCE_ID,
        organization_id=ORGANIZATION_ID,
        inspection_events=(
            InspectionEvent(
                event_id=uuid4(),
                organization_id=ORGANIZATION_ID,
                camera_id=uuid4(),
                occurred_at=OCCURRED_AT,
                defect_class="scratch",
                confidence=0.9,
                model_release="test-model",
                preprocessing_parameters=(),
                threshold=0.8,
                input_frame_sha256="a" * 64,
            ),
        ),
    )
    case_repository = InMemoryCaseRepository((case,))
    app = FastAPI()
    app.dependency_overrides[get_current_actor] = lambda: Actor(
        ACTOR_ID, ORGANIZATION_ID, Role.ADMINISTRATOR, frozenset()
    )
    app.include_router(
        create_cases_router(case_repository, audit_service=audit_service)
    )
    client = TestClient(app, raise_server_exceptions=False)

    successful = client.post(
        f"/api/v1/cases/{RESOURCE_ID}/transitions", json={"status": "IN_REVIEW"}
    )

    assert successful.status_code == 200
    audit_entries = repository.entries_for_organization(ORGANIZATION_ID)
    assert len(audit_entries) == 1
    assert audit_entries[0].resource_id == RESOURCE_ID
    assert audit_entries[0].action == "defect_case.transition"

    audit_service.block_appends(ORGANIZATION_ID)
    blocked = client.post(
        f"/api/v1/cases/{RESOURCE_ID}/transitions", json={"status": "RESOLVED"}
    )

    assert blocked.status_code == 503
    assert case_repository.get(RESOURCE_ID, ORGANIZATION_ID).case.status == "IN_REVIEW"
    assert len(repository.entries_for_organization(ORGANIZATION_ID)) == 1


def test_concurrent_case_transitions_commit_exactly_one_case_and_audit_entry() -> None:
    case = DefectCase(
        case_id=RESOURCE_ID,
        organization_id=ORGANIZATION_ID,
        inspection_events=(
            InspectionEvent(
                event_id=uuid4(),
                organization_id=ORGANIZATION_ID,
                camera_id=uuid4(),
                occurred_at=OCCURRED_AT,
                defect_class="scratch",
                confidence=0.9,
                model_release="test-model",
                preprocessing_parameters=(),
                threshold=0.8,
                input_frame_sha256="a" * 64,
            ),
        ),
    )
    case_repository = InMemoryCaseRepository((case,))
    audit_repository = InMemoryAuditRepository()
    audit_service = AuditService(audit_repository)
    actor = Actor(ACTOR_ID, ORGANIZATION_ID, Role.ADMINISTRATOR, frozenset())

    def transition_once() -> object:
        return case_repository.transition_with_audit(
            RESOURCE_ID,
            ORGANIZATION_ID,
            "IN_REVIEW",
            actor,
            audit_service,
            lambda before, after: command(action="defect_case.transition"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = (
            executor.submit(transition_once),
            executor.submit(transition_once),
        )
        outcomes = [future.exception() or future.result() for future in (first, second)]

    assert (
        sum(
            outcome is not None and not isinstance(outcome, Exception)
            for outcome in outcomes
        )
        == 1
    )
    assert sum(isinstance(outcome, InvalidCaseTransition) for outcome in outcomes) == 1
    assert case_repository.get(RESOURCE_ID, ORGANIZATION_ID).case.status == "IN_REVIEW"
    assert len(audit_repository.entries_for_organization(ORGANIZATION_ID)) == 1


def test_transition_transaction_preserves_forbidden_response_for_unauthorized_actor() -> (
    None
):
    case = DefectCase(
        case_id=RESOURCE_ID,
        organization_id=ORGANIZATION_ID,
        inspection_events=(
            InspectionEvent(
                event_id=uuid4(),
                organization_id=ORGANIZATION_ID,
                camera_id=uuid4(),
                occurred_at=OCCURRED_AT,
                defect_class="scratch",
                confidence=0.9,
                model_release="test-model",
                preprocessing_parameters=(),
                threshold=0.8,
                input_frame_sha256="a" * 64,
            ),
        ),
        line_id=uuid4(),
    )
    app = FastAPI()
    app.dependency_overrides[get_current_actor] = lambda: Actor(
        ACTOR_ID, ORGANIZATION_ID, Role.INSPECTOR, frozenset()
    )
    app.include_router(create_cases_router(InMemoryCaseRepository((case,))))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        f"/api/v1/cases/{RESOURCE_ID}/transitions", json={"status": "IN_REVIEW"}
    )

    assert response.status_code == 403
