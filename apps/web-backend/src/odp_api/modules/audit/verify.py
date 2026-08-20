"""Verification and operational health checks for audit chains."""

from dataclasses import dataclass
import logging
from typing import Protocol
from uuid import UUID

from odp_api.modules.audit.models import (
    AuditP0Failure,
    GENESIS_HASH,
    VerificationResult,
    calculate_entry_hash,
)
from odp_api.modules.audit.service import AuditAppendBlocked, AuditRepository, AuditService


def verify_organization_chain(
    organization_id: UUID, repository: AuditRepository
) -> VerificationResult:
    """Recalculate every entry and linkage in one organization's immutable chain."""
    expected_previous_hash = GENESIS_HASH
    expected_sequence = 1
    entries = repository.entries_for_organization(organization_id)
    for entry in entries:
        if entry.organization_id != organization_id:
            return VerificationResult(
                organization_id, False, expected_sequence - 1, entry.sequence, "organization mismatch"
            )
        if entry.sequence != expected_sequence:
            return VerificationResult(
                organization_id, False, expected_sequence - 1, entry.sequence, "sequence discontinuity"
            )
        if entry.previous_hash != expected_previous_hash:
            return VerificationResult(
                organization_id, False, expected_sequence - 1, entry.sequence, "previous hash mismatch"
            )
        expected_entry_hash = calculate_entry_hash(
            organization_id=entry.organization_id,
            sequence=entry.sequence,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            action=entry.action,
            change_summary=entry.change_summary,
            actor_id=entry.actor_id,
            occurred_at=entry.occurred_at,
            correlation_id=entry.correlation_id,
            request_ip=entry.request_ip,
            previous_hash=entry.previous_hash,
        )
        if entry.entry_hash != expected_entry_hash:
            return VerificationResult(
                organization_id, False, expected_sequence - 1, entry.sequence, "entry hash mismatch"
            )
        expected_previous_hash = entry.entry_hash
        expected_sequence += 1
    head = repository.chain_head_for_organization(organization_id)
    if head.last_sequence != expected_sequence - 1 or head.head_hash != expected_previous_hash:
        return VerificationResult(
            organization_id,
            False,
            expected_sequence - 1,
            None,
            "chain head mismatch",
        )
    return VerificationResult(organization_id, True, len(entries))


class P0FailureReporter(Protocol):
    def emit(self, failure: AuditP0Failure) -> None: ...


class LoggingP0FailureReporter:
    def emit(self, failure: AuditP0Failure) -> None:
        logging.getLogger(__name__).critical(
            "P0 audit hash-chain verification failure",
            extra={
                "audit_p0": True,
                "organization_id": str(failure.organization_id),
                "failed_sequence": failure.verification.failed_sequence,
                "reason": failure.verification.reason,
            },
        )


@dataclass(slots=True)
class InMemoryP0FailureReporter:
    failures: list[AuditP0Failure]

    def __init__(self) -> None:
        self.failures = []

    def emit(self, failure: AuditP0Failure) -> None:
        self.failures.append(failure)


class AuditVerificationMonitor:
    """Runs startup sampling and daily full checks, holding unsafe chains at P0."""

    def __init__(self, service: AuditService, reporter: P0FailureReporter | None = None) -> None:
        self._service = service
        self._reporter = reporter or LoggingP0FailureReporter()

    def startup_sample_verify(self, sample_size: int = 10) -> dict[UUID, VerificationResult]:
        organization_ids = sorted(self._service.repository.organization_ids(), key=str)[:sample_size]
        return self._verify_and_hold(organization_ids)

    def daily_full_verify(self) -> dict[UUID, VerificationResult]:
        return self._verify_and_hold(self._service.repository.organization_ids())

    def _verify_and_hold(self, organization_ids: tuple[UUID, ...] | list[UUID]) -> dict[UUID, VerificationResult]:
        results = {
            organization_id: self._service.verify_organization_chain(organization_id)
            for organization_id in organization_ids
        }
        for organization_id, result in results.items():
            if not result.is_valid:
                self._service.block_appends(organization_id)
                self._reporter.emit(AuditP0Failure(organization_id, result))
        return results


__all__ = [
    "AuditAppendBlocked",
    "AuditVerificationMonitor",
    "InMemoryP0FailureReporter",
    "verify_organization_chain",
]
