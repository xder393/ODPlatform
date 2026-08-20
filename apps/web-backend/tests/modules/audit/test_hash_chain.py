from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
import sys
from uuid import UUID, uuid4

import pytest


WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC)]

from odp_api.modules.audit.models import AuditCommand
from odp_api.modules.audit.service import AuditService, InMemoryAuditRepository
from odp_api.modules.audit.verify import (
    AuditAppendBlocked,
    AuditVerificationMonitor,
    InMemoryP0FailureReporter,
)
from odp_api.modules.cases.router import InMemoryCaseRepository, create_cases_router
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import get_current_actor
from odp_api.modules.inspection.models import DefectCase, InspectionEvent
from fastapi import FastAPI
from fastapi.testclient import TestClient


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
    assert entry.entry_hash == "dc39d1bb627353f5dbfaf9e15dab89f3931cc99438da563b06f89f5b239a5316"


def test_concurrent_appends_form_one_continuous_organization_chain() -> None:
    service = AuditService(InMemoryAuditRepository())

    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = list(
            executor.map(
                lambda index: service.append(command(action=f"case.transition.{index}")),
                range(20),
            )
        )

    ordered = sorted(entries, key=lambda entry: entry.sequence)
    assert [entry.sequence for entry in ordered] == list(range(1, 21))
    assert all(
        current.previous_hash == previous.entry_hash
        for previous, current in zip(ordered, ordered[1:])
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


def test_failed_startup_verification_emits_p0_and_blocks_appends_until_recovery() -> None:
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

    service.explicit_administrator_recovery(ORGANIZATION_ID)
    assert service.append(command(action="case.resolve")).sequence == 2


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
    app.include_router(create_cases_router(case_repository, audit_service=audit_service))
    client = TestClient(app, raise_server_exceptions=False)

    successful = client.post(f"/api/v1/cases/{RESOURCE_ID}/transitions", json={"status": "IN_REVIEW"})

    assert successful.status_code == 200
    audit_entries = repository.entries_for_organization(ORGANIZATION_ID)
    assert len(audit_entries) == 1
    assert audit_entries[0].resource_id == RESOURCE_ID
    assert audit_entries[0].action == "defect_case.transition"

    audit_service.block_appends(ORGANIZATION_ID)
    blocked = client.post(f"/api/v1/cases/{RESOURCE_ID}/transitions", json={"status": "RESOLVED"})

    assert blocked.status_code == 503
    assert case_repository.get(RESOURCE_ID, ORGANIZATION_ID).case.status == "IN_REVIEW"
    assert len(repository.entries_for_organization(ORGANIZATION_ID)) == 1
