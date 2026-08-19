from dataclasses import replace
from typing import Final
from uuid import UUID

from odp_api.modules.cases.errors import InvalidCaseTransition
from odp_api.modules.inspection.models import CaseStatus, DefectCase

ALLOWED_TRANSITIONS: Final[dict[CaseStatus, frozenset[CaseStatus]]] = {
    "PENDING_CONFIRMATION": frozenset({"IN_REVIEW", "FALSE_POSITIVE"}),
    "IN_REVIEW": frozenset({"RESOLVED", "FALSE_POSITIVE"}),
    "RESOLVED": frozenset(),
    "FALSE_POSITIVE": frozenset(),
}


class CaseService:
    """Pure case-domain operations with no persistence or provider dependencies."""

    @staticmethod
    def transition(case: DefectCase, to_status: CaseStatus, actor_id: UUID) -> DefectCase:
        """Return a new case after an allowed state transition."""
        if to_status not in ALLOWED_TRANSITIONS[case.status]:
            raise InvalidCaseTransition(case.status, to_status)

        return replace(case, status=to_status, last_transition_actor_id=actor_id)
