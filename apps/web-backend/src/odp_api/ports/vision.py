from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from odp_api.modules.tasks.commands import Detection, InferenceExecutionContract


@dataclass(frozen=True, slots=True)
class FrameInput:
    """Frame data and its stable fixture identifier for a vision inference."""

    fixture_name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class VisionResult:
    """A complete, reproducible result returned by a vision provider."""

    execution: InferenceExecutionContract
    frame_sha256: str
    detections: tuple[Detection, ...]
    stage_durations: tuple[tuple[str, float], ...] = ()
    legacy_preprocessing_parameters: tuple[tuple[str, str], ...] | None = None

    @property
    def defect_class(self) -> str:
        return self.detections[0].defect_type or self.detections[0].class_name if self.detections else "unknown"

    @property
    def confidence(self) -> float:
        return self.detections[0].confidence if self.detections else 0.0

    @property
    def model_release(self) -> str:
        return self.execution.model_release

    @property
    def preprocessing_parameters(self) -> tuple[tuple[str, str], ...]:
        if self.legacy_preprocessing_parameters is not None:
            return self.legacy_preprocessing_parameters
        return (
            ("preprocessing_version", self.execution.preprocessing_version),
            ("postprocessing_version", self.execution.postprocessing_version),
        )

    @property
    def threshold(self) -> float:
        return self.execution.confidence_threshold

    @property
    def input_frame_sha256(self) -> str:
        return self.frame_sha256


class VisionInferencePort(Protocol):
    """Boundary for inference providers used by the inspection application service."""

    def inspect(
        self,
        frame: FrameInput,
        contract: InferenceExecutionContract | None = None,
    ) -> VisionResult: ...


def default_mock_contract() -> InferenceExecutionContract:
    """Stable offline contract used by the P0 fixture path."""

    return InferenceExecutionContract(
        model_release="mock-yolo-1.0",
        model_sha256=sha256(b"mock-yolo-1.0").hexdigest(),
        onnxruntime_version="mock-runtime-1",
        execution_provider="mock",
        actual_input_shape=(1, 3, 640, 640),
        preprocessing_version="fixture-bytes-v1",
        postprocessing_version="mock-nms-v1",
        confidence_threshold=0.80,
        iou_threshold=0.45,
        nms_mode="post_nms_xyxy",
        nms_in_model=True,
        class_map_version="mock-classes-v1",
    )
