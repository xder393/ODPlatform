"""Immutable messages exchanged by the P1 inference control plane."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
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


class DeliveryOutcome(StrEnum):
    """Authoritative PostgreSQL disposition for one Redis delivery."""

    CLAIMED = "CLAIMED"
    PENDING = "PENDING"
    DUPLICATE = "DUPLICATE"
    QUARANTINED = "QUARANTINED"


class QuarantineReason(StrEnum):
    """Stable compatibility/integrity reasons persisted for poison deliveries."""

    MALFORMED_ENVELOPE = "MALFORMED_ENVELOPE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    UNRESOLVED_TASK_REFERENCE = "UNRESOLVED_TASK_REFERENCE"
    FUTURE_DISPATCH_SEQUENCE = "FUTURE_DISPATCH_SEQUENCE"


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    """Bounded reference delivery checked atomically against task authority."""

    stream_name: str
    message_id: str
    event_id: UUID
    event_type: str
    schema_version: str
    raw_payload: bytes
    organization_id: UUID
    task_id: UUID
    expected_dispatch_seq: int
    quarantine_reason: QuarantineReason | None = None

    def __post_init__(self) -> None:
        if self.expected_dispatch_seq < 1:
            raise ValueError("expected_dispatch_seq must be positive")
        if len(self.raw_payload) > 65536:
            raise ValueError("raw_payload exceeds the quarantine bound")


@dataclass(frozen=True, slots=True)
class UnscopedQuarantineCommand:
    """A poison message whose task authority cannot safely be recovered."""

    stream_name: str
    message_id: str
    event_id: UUID
    event_type: str
    schema_version: str
    raw_payload: bytes
    reason: QuarantineReason
    organization_id: UUID | None = None

    def __post_init__(self) -> None:
        if len(self.raw_payload) > 65536:
            raise ValueError("raw_payload exceeds the quarantine bound")


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    """Database-committed delivery decision returned to the Worker."""

    outcome: DeliveryOutcome
    claim: LeaseClaim | None = None

    def __post_init__(self) -> None:
        if (self.outcome is DeliveryOutcome.CLAIMED) != (self.claim is not None):
            raise ValueError("only CLAIMED decisions carry a lease claim")


@dataclass(frozen=True, slots=True)
class WorkerDeliveryScope:
    """Capability authorizing one Worker identity to quarantine unscoped bytes."""

    worker_id: str

    def __post_init__(self) -> None:
        if not self.worker_id.strip():
            raise ValueError("worker_id is required")
