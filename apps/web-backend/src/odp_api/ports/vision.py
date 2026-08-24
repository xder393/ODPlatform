from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FrameInput:
    """Frame data and its stable fixture identifier for a vision inference."""

    fixture_name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class VisionResult:
    """A reproducible defect detection returned by a vision provider."""

    defect_class: str
    confidence: float
    model_release: str
    preprocessing_parameters: tuple[tuple[str, str], ...]
    threshold: float
    input_frame_sha256: str


class VisionInferencePort(Protocol):
    """Boundary for inference providers used by the inspection application service."""

    def inspect(self, frame: FrameInput) -> VisionResult: ...
