from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from odp_api.modules.cases.errors import InvalidCaseStatus

from odp_schemas.events import InspectionAlert

CaseStatus = Literal["PENDING_CONFIRMATION", "IN_REVIEW", "RESOLVED", "FALSE_POSITIVE"]
CASE_STATUSES: frozenset[CaseStatus] = frozenset(
    {"PENDING_CONFIRMATION", "IN_REVIEW", "RESOLVED", "FALSE_POSITIVE"}
)


@dataclass(frozen=True, slots=True)
class InspectionEvent:
    """An immutable, reproducible record of a model inspection result."""

    event_id: UUID
    organization_id: UUID
    camera_id: UUID
    occurred_at: datetime
    defect_class: str
    confidence: float
    model_release: str
    preprocessing_parameters: tuple[tuple[str, str], ...]
    threshold: float
    input_frame_sha256: str
    line_id: UUID | None = None

    @classmethod
    def from_alert(
        cls,
        alert: InspectionAlert,
        *,
        model_release: str,
        preprocessing_parameters: tuple[tuple[str, str], ...],
        threshold: float,
        input_frame_sha256: str,
        line_id: UUID | None = None,
    ) -> "InspectionEvent":
        """Snapshot a shared inspection alert with reproducibility metadata."""
        return cls(
            event_id=alert.event_id,
            organization_id=alert.organization_id,
            camera_id=alert.camera_id,
            occurred_at=alert.occurred_at,
            defect_class=alert.defect_class,
            confidence=alert.confidence,
            model_release=model_release,
            preprocessing_parameters=preprocessing_parameters,
            threshold=threshold,
            input_frame_sha256=input_frame_sha256,
            line_id=line_id,
        )

    def to_alert(self) -> InspectionAlert:
        """Return the shared realtime contract for this immutable event."""
        return InspectionAlert(
            event_id=self.event_id,
            organization_id=self.organization_id,
            camera_id=self.camera_id,
            occurred_at=self.occurred_at,
            defect_class=self.defect_class,
            confidence=self.confidence,
        )


@dataclass(frozen=True, slots=True)
class DefectCase:
    """An immutable aggregate for the human disposition of inspection events."""

    case_id: UUID
    organization_id: UUID
    inspection_events: tuple[InspectionEvent, ...]
    status: CaseStatus = "PENDING_CONFIRMATION"
    assignee_id: UUID | None = None
    last_transition_actor_id: UUID | None = None
    line_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.status not in CASE_STATUSES:
            raise InvalidCaseStatus(self.status)
        if not self.inspection_events:
            raise ValueError("A defect case requires at least one inspection event.")
        if any(event.organization_id != self.organization_id for event in self.inspection_events):
            raise ValueError("All inspection events must belong to the case organization.")
