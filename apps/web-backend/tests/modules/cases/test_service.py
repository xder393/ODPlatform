from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path
import sys
from uuid import uuid4

import pytest


WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[5] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.modules.cases.errors import InvalidCaseStatus, InvalidCaseTransition
from odp_api.modules.cases.service import CaseService
from odp_api.modules.inspection.models import DefectCase, InspectionEvent
from odp_schemas.events import InspectionAlert


def make_case() -> DefectCase:
    alert = InspectionAlert(
        event_id=uuid4(),
        organization_id=uuid4(),
        camera_id=uuid4(),
        occurred_at=datetime.now(UTC),
        defect_class="scratch",
        confidence=0.964,
    )
    event = InspectionEvent.from_alert(
        alert,
        model_release="mock-yolo-1.0",
        preprocessing_parameters=(),
        threshold=0.80,
        input_frame_sha256="a" * 64,
    )
    return DefectCase(
        case_id=uuid4(),
        organization_id=alert.organization_id,
        inspection_events=(event,),
    )


def test_pending_confirmation_can_move_to_in_review() -> None:
    case = make_case()

    transitioned = CaseService.transition(case, "IN_REVIEW", uuid4())

    assert transitioned.status == "IN_REVIEW"
    assert transitioned.last_transition_actor_id is not None
    assert case.status == "PENDING_CONFIRMATION"


def test_in_review_can_move_to_resolved() -> None:
    case = make_case()
    in_review = CaseService.transition(case, "IN_REVIEW", uuid4())

    resolved = CaseService.transition(in_review, "RESOLVED", uuid4())

    assert resolved.status == "RESOLVED"


def test_resolved_case_cannot_reopen_for_review() -> None:
    case = make_case()
    in_review = CaseService.transition(case, "IN_REVIEW", uuid4())
    resolved = CaseService.transition(in_review, "RESOLVED", uuid4())

    with pytest.raises(InvalidCaseTransition):
        CaseService.transition(resolved, "IN_REVIEW", uuid4())


def test_case_rejects_an_unknown_status_at_construction() -> None:
    case = make_case()

    with pytest.raises(InvalidCaseStatus):
        replace(case, status="OPEN")
