from hashlib import sha256

from odp_api.modules.tasks.commands import Detection, InferenceExecutionContract
from odp_api.ports.vision import FrameInput, VisionResult, default_mock_contract


class MockVisionAdapter:
    """Deterministic fixture-only implementation of the vision inference port."""

    def inspect(
        self,
        frame: FrameInput,
        contract: InferenceExecutionContract | None = None,
    ) -> VisionResult:
        execution = contract or default_mock_contract()
        frame_hash = sha256(frame.content).hexdigest()
        detection = Detection(
            class_id=0,
            class_name="scratch",
            defect_type="scratch",
            severity="MEDIUM",
            confidence=0.964,
            xyxy=(0.1, 0.1, 0.9, 0.9),
            spatial_zone="GLOBAL",
        )
        return VisionResult(
            execution=execution,
            frame_sha256=frame_hash,
            detections=(detection,),
            stage_durations=(("mock", 0.0),),
            legacy_preprocessing_parameters=(("fixture", frame.fixture_name),),
        )


class DeterministicMockVisionAdapter(MockVisionAdapter):
    """Named Task 6 adapter while preserving the P0 ``MockVisionAdapter`` API."""
