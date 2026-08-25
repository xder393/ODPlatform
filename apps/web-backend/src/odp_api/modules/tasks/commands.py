"""Immutable messages exchanged by the P1 inference control plane."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LeaseClaim:
    """Database fencing ownership granted to one worker attempt."""

    task_id: UUID
    organization_id: UUID
    artifact_id: UUID
    attempt_id: UUID
    attempt_no: int
    fence_token: int
    lease_owner: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class InferenceExecutionContract:
    """All model/runtime parameters needed to reproduce an inference."""

    model_release: str
    model_sha256: str
    onnxruntime_version: str
    execution_provider: str
    actual_input_shape: tuple[int, ...]
    preprocessing_version: str
    postprocessing_version: str
    confidence_threshold: float
    iou_threshold: float
    nms_mode: str
    nms_in_model: bool
    class_map_version: str


@dataclass(frozen=True, slots=True)
class PublishInferenceCommand:
    """Fenced inference output handed to the inspection effect boundary."""

    claim: LeaseClaim
    execution_contract: InferenceExecutionContract
    frame_sha256: str
    detections: tuple[dict[str, object], ...]
    stage_durations: tuple[tuple[str, float], ...]
    correlation_id: UUID
    database_completed_at: datetime
