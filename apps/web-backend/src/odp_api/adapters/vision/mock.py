from hashlib import sha256

from odp_api.ports.vision import FrameInput, VisionResult


class MockVisionAdapter:
    """Deterministic fixture-only implementation of the vision inference port."""

    def inspect(self, frame: FrameInput) -> VisionResult:
        return VisionResult(
            defect_class="scratch",
            confidence=0.964,
            model_release="mock-yolo-1.0",
            preprocessing_parameters=(("fixture", frame.fixture_name),),
            threshold=0.80,
            input_frame_sha256=sha256(frame.content).hexdigest(),
        )
