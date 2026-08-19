from datetime import UTC, datetime
from uuid import UUID, uuid4

from odp_api.modules.inspection.models import DefectCase, InspectionEvent
from odp_api.ports.vision import FrameInput, VisionInferencePort
from odp_schemas.events import InspectionAlert


class InspectionService:
    """Creates inspection cases from inference results without provider coupling."""

    def __init__(self, vision: VisionInferencePort) -> None:
        self._vision = vision

    def inspect_fixture(
        self, frame: FrameInput, organization_id: UUID, camera_id: UUID
    ) -> DefectCase:
        result = self._vision.inspect(frame)
        alert = InspectionAlert(
            event_id=uuid4(),
            organization_id=organization_id,
            camera_id=camera_id,
            occurred_at=datetime.now(UTC),
            defect_class=result.defect_class,
            confidence=result.confidence,
        )
        event = InspectionEvent.from_alert(
            alert,
            model_release=result.model_release,
            preprocessing_parameters=result.preprocessing_parameters,
            threshold=result.threshold,
            input_frame_sha256=result.input_frame_sha256,
        )
        return DefectCase(
            case_id=uuid4(), organization_id=organization_id, inspection_events=(event,)
        )
