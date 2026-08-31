"""Recovery-aware Redis inference Worker with PostgreSQL delivery authority."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from odp_schemas.events import EventEnvelope
from pydantic import ValidationError

from odp_api.modules.tasks.commands import (
    DeliveryDecision,
    DeliveryOutcome,
    DeliveryRequest,
    Detection,
    InferenceExecutionContract,
    LeaseClaim,
    PublishInferenceCommand,
    QuarantineReason,
    UnscopedQuarantineCommand,
    WorkerDeliveryScope,
)
from odp_api.modules.tasks.consumer import (
    GROUP_NAME,
    MAX_ENVELOPE_BYTES,
    RedisInferenceConsumer,
    RedisInferenceMessage,
)
from odp_api.modules.tasks.models import FailureKind
from odp_api.modules.tasks.recovery import QuarantineResult
from odp_api.ports.tasks import StaleLease

LEASE_SECONDS = 20
RENEW_INTERVAL_SECONDS = 5
INFERENCE_TIMEOUT_SECONDS = 10
RECOVERY_INTERVAL_SECONDS = 2
SUPPORTED_EVENT_TYPE = "vision.inference.requested.v1"
SUPPORTED_SCHEMA_VERSION = 1
LOGGER = logging.getLogger(__name__)
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InvalidInputError(ValueError):
    """An artifact or frame cannot be interpreted safely."""


class ModelConfigurationError(RuntimeError):
    """A model adapter did not return the frozen execution contract."""


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    """Verified immutable bytes loaded for one claimed Artifact."""

    content: bytes
    sha256: str
    object_key: str | None = None


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """Complete typed output required by the inspection-effect boundary."""

    execution_contract: InferenceExecutionContract
    frame_sha256: str
    detections: tuple[Detection, ...]
    stage_durations: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class WorkerProcessResult:
    """Observable result of handling one Redis delivery."""

    outcome: str
    acked: bool
    claim: LeaseClaim | None = None


class WorkerTaskControlPort(Protocol):
    """Async, explicit database boundary used by the event-loop Worker."""

    worker_id: str

    async def accept_delivery(
        self, request: DeliveryRequest, now: datetime
    ) -> DeliveryDecision: ...

    async def quarantine_unscoped(
        self, command: UnscopedQuarantineCommand, now: datetime
    ) -> QuarantineResult: ...

    async def renew(self, claim: LeaseClaim, now: datetime) -> LeaseClaim | None: ...

    async def record_failure(
        self, claim: LeaseClaim, failure: FailureKind, now: datetime
    ) -> None: ...


class SynchronousWorkerTaskControl(Protocol):
    """Synchronous repository contract offloaded by the process adapter."""

    def accept_delivery(
        self,
        request: DeliveryRequest,
        worker_id: str,
        now: datetime,
        scope: WorkerDeliveryScope,
    ) -> DeliveryDecision: ...

    def quarantine_unscoped(
        self,
        command: UnscopedQuarantineCommand,
        now: datetime,
        scope: WorkerDeliveryScope,
    ) -> QuarantineResult: ...

    def renew(self, claim: LeaseClaim, now: datetime) -> LeaseClaim | None: ...

    def record_failure(
        self, claim: LeaseClaim, failure: FailureKind, now: datetime
    ) -> None: ...


class ArtifactLoaderPort(Protocol):
    """Async Artifact adapter; synchronous SDKs must offload inside their adapter."""

    async def load(self, claim: LeaseClaim) -> LoadedArtifact: ...


class InferencePort(Protocol):
    """Async model adapter; CPU-bound runtimes must offload inside their adapter."""

    async def infer(
        self, artifact: LoadedArtifact, claim: LeaseClaim
    ) -> InferenceResult: ...


class InspectionEffectPort(Protocol):
    """Async success boundary; only this port may publish business effects."""

    async def publish(self, command: PublishInferenceCommand) -> object: ...


class SynchronousInspectionEffects(Protocol):
    def publish(self, command: PublishInferenceCommand) -> object: ...


class ThreadedTaskControlAdapter:
    """Offload the synchronous SQLAlchemy repository from the event loop."""

    def __init__(self, repository: SynchronousWorkerTaskControl, worker_id: str) -> None:
        self.worker_id = worker_id
        self._repository = repository
        self._scope = WorkerDeliveryScope(worker_id)

    async def accept_delivery(
        self, request: DeliveryRequest, now: datetime
    ) -> DeliveryDecision:
        return await asyncio.to_thread(
            self._repository.accept_delivery,
            request,
            self.worker_id,
            now,
            self._scope,
        )

    async def quarantine_unscoped(
        self, command: UnscopedQuarantineCommand, now: datetime
    ) -> QuarantineResult:
        return await asyncio.to_thread(
            self._repository.quarantine_unscoped,
            command,
            now,
            self._scope,
        )

    async def renew(self, claim: LeaseClaim, now: datetime) -> LeaseClaim | None:
        return await asyncio.to_thread(self._repository.renew, claim, now)

    async def record_failure(
        self, claim: LeaseClaim, failure: FailureKind, now: datetime
    ) -> None:
        await asyncio.to_thread(self._repository.record_failure, claim, failure, now)


class ThreadedInspectionEffectAdapter:
    """Offload the synchronous effect transaction from the event loop."""

    def __init__(self, effects: SynchronousInspectionEffects) -> None:
        self._effects = effects

    async def publish(self, command: PublishInferenceCommand) -> object:
        return await asyncio.to_thread(self._effects.publish, command)


class _RenewalState:
    def __init__(self, claim: LeaseClaim) -> None:
        self.claim = claim
        self.lost = asyncio.Event()
        self.error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _EnvelopeMetadata:
    event_id: UUID
    event_type: str
    schema_version: str
    organization_id: UUID | None
    task_id: UUID | None
    dispatch_seq: int | None


class InferenceWorker:
    """Process typed Redis deliveries through one explicit set of async ports."""

    def __init__(
        self,
        consumer: RedisInferenceConsumer,
        task_control: WorkerTaskControlPort,
        effects: InspectionEffectPort,
        artifact_loader: ArtifactLoaderPort,
        inference: InferencePort,
        *,
        worker_id: str | None = None,
        clock: Clock | None = None,
        renewal_interval_seconds: float = RENEW_INTERVAL_SECONDS,
        inference_timeout_seconds: float = INFERENCE_TIMEOUT_SECONDS,
        recovery_interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
    ) -> None:
        self._consumer = consumer
        self._control = task_control
        self._effects = effects
        self._artifact_loader = artifact_loader
        self._inference = inference
        self.worker_id = worker_id or task_control.worker_id
        if not self.worker_id.strip():
            raise ValueError("worker_id is required")
        if self.worker_id != task_control.worker_id:
            raise ValueError("Worker identity must match its task-control capability")
        if renewal_interval_seconds <= 0:
            raise ValueError("renewal_interval_seconds must be positive")
        if inference_timeout_seconds <= 0:
            raise ValueError("inference_timeout_seconds must be positive")
        if recovery_interval_seconds <= 0:
            raise ValueError("recovery_interval_seconds must be positive")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be a zero-argument callable")
        self.renewal_interval_seconds = renewal_interval_seconds
        self.inference_timeout_seconds = inference_timeout_seconds
        self.recovery_interval_seconds = recovery_interval_seconds
        self._clock: Clock = _utc_now if clock is None else clock
        self._active_renewals: set[asyncio.Task[None]] = set()

    async def process(self, message: RedisInferenceMessage) -> WorkerProcessResult:
        """Handle one normalized delivery and preserve commit-before-ACK ordering."""

        raw_payload = message.raw_payload
        bounded_payload = raw_payload[:MAX_ENVELOPE_BYTES]
        metadata = _metadata_from_payload(bounded_payload, message.message_id, message.stream)
        if len(raw_payload) > MAX_ENVELOPE_BYTES:
            return await self._quarantine_delivery(
                message,
                bounded_payload,
                metadata,
                QuarantineReason.PAYLOAD_TOO_LARGE,
            )

        try:
            envelope = EventEnvelope.model_validate_json(raw_payload)
        except (ValidationError, ValueError, TypeError, UnicodeDecodeError):
            return await self._quarantine_delivery(
                message,
                bounded_payload,
                metadata,
                QuarantineReason.MALFORMED_ENVELOPE,
            )

        metadata = _metadata_from_envelope(envelope)
        if (
            envelope.event_type != SUPPORTED_EVENT_TYPE
            or envelope.schema_version != SUPPORTED_SCHEMA_VERSION
        ):
            return await self._quarantine_delivery(
                message,
                bounded_payload,
                metadata,
                QuarantineReason.UNSUPPORTED_SCHEMA,
                scoped=(
                    metadata.organization_id is not None
                    and metadata.task_id is not None
                    and metadata.dispatch_seq is not None
                    and metadata.dispatch_seq > 0
                ),
            )

        if (
            metadata.organization_id is None
            or metadata.task_id is None
            or metadata.dispatch_seq is None
            or metadata.dispatch_seq < 1
        ):
            return await self._quarantine_delivery(
                message,
                bounded_payload,
                metadata,
                QuarantineReason.MALFORMED_ENVELOPE,
            )

        request = _delivery_request(message, bounded_payload, metadata, None)
        decision = await self._control.accept_delivery(request, self._now())
        if decision.outcome is DeliveryOutcome.DUPLICATE:
            await self._consumer.ack(message.message_id)
            return WorkerProcessResult("DUPLICATE", True)
        if decision.outcome is DeliveryOutcome.QUARANTINED:
            await self._consumer.ack(message.message_id)
            return WorkerProcessResult("QUARANTINED", True)
        if decision.outcome is DeliveryOutcome.PENDING:
            return WorkerProcessResult("PENDING", False)
        claim = decision.claim
        if decision.outcome is not DeliveryOutcome.CLAIMED or claim is None:
            raise RuntimeError("task-control port returned an invalid delivery decision")

        renewal = _RenewalState(claim)
        renewal_task = asyncio.create_task(self._renew_loop(renewal))
        self._active_renewals.add(renewal_task)
        try:
            await asyncio.sleep(0)
            try:
                await self._run_claimed(renewal, envelope)
            except asyncio.CancelledError:
                raise
            except StaleLease:
                return WorkerProcessResult("PENDING", False, claim)
            except Exception as error:  # noqa: BLE001 - classified at this boundary
                failure = _classify_failure(error)
                if failure is None:
                    return WorkerProcessResult("PENDING", False, claim)
                await self._control.record_failure(renewal.claim, failure, self._now())
                await self._consumer.ack(message.message_id)
                return WorkerProcessResult("FAILED", True, renewal.claim)

            await self._consumer.ack(message.message_id)
            return WorkerProcessResult("SUCCEEDED", True, renewal.claim)
        finally:
            renewal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal_task
            self._active_renewals.discard(renewal_task)

    async def _quarantine_delivery(
        self,
        message: RedisInferenceMessage,
        raw_payload: bytes,
        metadata: _EnvelopeMetadata,
        reason: QuarantineReason,
        *,
        scoped: bool = False,
    ) -> WorkerProcessResult:
        if scoped:
            # This branch is reached only after EventEnvelope validation and
            # complete reference validation.  The repository still performs
            # the sequence comparison and task transition atomically.
            decision = await self._control.accept_delivery(
                _delivery_request(message, raw_payload, metadata, reason),
                self._now(),
            )
            if decision.outcome is DeliveryOutcome.PENDING:
                return WorkerProcessResult("PENDING", False)
            if decision.outcome is DeliveryOutcome.CLAIMED:
                raise RuntimeError("quarantine delivery unexpectedly acquired a lease")
        else:
            # Until EventEnvelope validation succeeds, extracted identifiers
            # are observation only.  Malformed and oversized bytes can never
            # select a task-scoped state transition.
            result = await self._control.quarantine_unscoped(
                UnscopedQuarantineCommand(
                    stream_name=message.stream,
                    message_id=message.message_id,
                    event_id=metadata.event_id,
                    event_type=metadata.event_type,
                    schema_version=metadata.schema_version,
                    raw_payload=raw_payload,
                    reason=reason,
                    organization_id=metadata.organization_id,
                ),
                self._now(),
            )
            if not result.ack_after_commit:
                return WorkerProcessResult("QUARANTINED", False)
        await self._consumer.ack(message.message_id)
        return WorkerProcessResult("QUARANTINED", True)

    async def _run_claimed(
        self, renewal: _RenewalState, envelope: EventEnvelope
    ) -> None:
        artifact = await self._artifact_loader.load(renewal.claim)
        _validate_artifact(artifact)
        _raise_renewal_issue(renewal)
        result = await _run_with_deadline(
            self._inference.infer(artifact, renewal.claim),
            self.inference_timeout_seconds,
        )
        detections = _validate_result(result, artifact)
        _raise_renewal_issue(renewal)
        await self._effects.publish(
            PublishInferenceCommand(
                claim=renewal.claim,
                execution_contract=result.execution_contract,
                frame_sha256=result.frame_sha256,
                detections=tuple(detection.as_dict() for detection in detections),
                stage_durations=result.stage_durations,
                correlation_id=envelope.correlation_id,
                database_completed_at=self._now(),
            )
        )
        _raise_renewal_issue(renewal)

    async def run_once(self) -> int:
        """Recover stale deliveries and process a new batch with error isolation."""

        await self._consumer.start()
        recovered = await self._consumer.claim_stale()
        new_messages = await self._consumer.read_new()
        messages = [*recovered, *new_messages]
        for message in messages:
            try:
                await self.process(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "inference delivery failed; leaving message pending",
                    extra={"redis_message_id": message.message_id},
                )
                continue
        return len(messages)

    async def recovery_once(self) -> int:
        await self._consumer.start()
        messages = await self._consumer.claim_stale()
        for message in messages:
            try:
                await self.process(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "recovered inference delivery failed; leaving message pending",
                    extra={"redis_message_id": message.message_id},
                )
                continue
        return len(messages)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        await self._consumer.start()
        while stop_event is None or not stop_event.is_set():
            await self.run_once()
            if stop_event is None:
                await asyncio.sleep(self.recovery_interval_seconds)
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.recovery_interval_seconds
                    )

    async def close(self) -> None:
        renewals = tuple(self._active_renewals)
        for task in renewals:
            task.cancel()
        for task in renewals:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._consumer.close()

    async def _renew_loop(self, state: _RenewalState) -> None:
        try:
            while True:
                await asyncio.sleep(self.renewal_interval_seconds)
                renewed = await self._control.renew(state.claim, self._now())
                if renewed is None:
                    state.lost.set()
                    return
                state.claim = renewed
        except asyncio.CancelledError:
            raise
        except StaleLease as error:
            state.error = error
            state.lost.set()
        except Exception as error:  # noqa: BLE001 - surfaced at claimed boundary
            state.error = error
            state.lost.set()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _run_with_deadline(coroutine: Awaitable[InferenceResult], seconds: float) -> InferenceResult:
    async with asyncio.timeout(seconds):
        return await coroutine


def _delivery_request(
    message: RedisInferenceMessage,
    raw_payload: bytes,
    metadata: _EnvelopeMetadata,
    reason: QuarantineReason | None,
) -> DeliveryRequest:
    if (
        metadata.organization_id is None
        or metadata.task_id is None
        or metadata.dispatch_seq is None
    ):
        raise ValueError("scoped delivery metadata is incomplete")
    return DeliveryRequest(
        stream_name=message.stream,
        message_id=message.message_id,
        event_id=metadata.event_id,
        event_type=metadata.event_type,
        schema_version=metadata.schema_version,
        raw_payload=raw_payload,
        organization_id=metadata.organization_id,
        task_id=metadata.task_id,
        expected_dispatch_seq=metadata.dispatch_seq,
        quarantine_reason=reason,
    )


def _metadata_from_payload(raw: bytes, message_id: str, stream: str) -> _EnvelopeMetadata:
    event_id = _regex_uuid(raw, b"event_id") or uuid5(NAMESPACE_URL, f"{stream}:{message_id}")
    return _EnvelopeMetadata(
        event_id=event_id,
        event_type=_regex_string(raw, b"event_type") or "unknown",
        schema_version=_regex_scalar(raw, b"schema_version") or "unknown",
        organization_id=_regex_uuid(raw, b"organization_id"),
        task_id=_regex_uuid(raw, b"task_id"),
        dispatch_seq=_regex_int(raw, b"dispatch_seq"),
    )


def _metadata_from_envelope(envelope: EventEnvelope) -> _EnvelopeMetadata:
    task_id = _payload_uuid(envelope.payload.get("task_id"))
    dispatch = envelope.payload.get("dispatch_seq")
    return _EnvelopeMetadata(
        envelope.event_id,
        envelope.event_type,
        str(envelope.schema_version),
        envelope.organization_id,
        task_id,
        dispatch if type(dispatch) is int else None,
    )


def _regex_uuid(raw: bytes, key: bytes) -> UUID | None:
    pattern = rb'["\']?' + re.escape(key) + rb'["\']?\s*:\s*["\']([0-9a-fA-F-]{36})'
    match = re.search(pattern, raw)
    if match is None:
        return None
    try:
        return UUID(match.group(1).decode())
    except (ValueError, UnicodeDecodeError):
        return None


def _regex_string(raw: bytes, key: bytes) -> str | None:
    pattern = rb'["\']?' + re.escape(key) + rb'["\']?\s*:\s*["\']([^"\']*)'
    match = re.search(pattern, raw)
    return match.group(1).decode("utf-8", errors="replace") if match else None


def _regex_scalar(raw: bytes, key: bytes) -> str | None:
    string_value = _regex_string(raw, key)
    if string_value is not None:
        return string_value
    pattern = rb'["\']?' + re.escape(key) + rb'["\']?\s*:\s*(-?\d+)'
    match = re.search(pattern, raw)
    return match.group(1).decode() if match else None


def _regex_int(raw: bytes, key: bytes) -> int | None:
    value = _regex_scalar(raw, key)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _payload_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _validate_artifact(artifact: LoadedArtifact) -> None:
    if not isinstance(artifact, LoadedArtifact):
        raise InvalidInputError("artifact loader must return LoadedArtifact")
    if not _is_sha256(artifact.sha256):
        raise InvalidInputError("artifact SHA-256 is invalid")
    if hashlib.sha256(artifact.content).hexdigest() != artifact.sha256.lower():
        raise InvalidInputError("artifact SHA-256 does not match content")


def _validate_result(
    result: InferenceResult, artifact: LoadedArtifact
) -> tuple[Detection, ...]:
    if not isinstance(result, InferenceResult):
        raise ModelConfigurationError("inference adapter must return InferenceResult")
    contract = result.execution_contract
    if not isinstance(contract, InferenceExecutionContract):
        raise ModelConfigurationError("inference execution contract is missing")
    required_text = (
        (contract.model_release, 255),
        (contract.onnxruntime_version, 64),
        (contract.execution_provider, 128),
        (contract.preprocessing_version, 128),
        (contract.postprocessing_version, 128),
        (contract.nms_mode, 64),
        (contract.class_map_version, 128),
    )
    if any(
        not isinstance(value, str)
        or not value.strip()
        or value.strip().lower() == "unknown"
        or "\x00" in value
        or len(value) > limit
        for value, limit in required_text
    ):
        raise ModelConfigurationError("inference execution contract is incomplete")
    if not _is_sha256(contract.model_sha256):
        raise ModelConfigurationError("model SHA-256 is invalid")
    if (
        not isinstance(contract.actual_input_shape, tuple)
        or not contract.actual_input_shape
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in contract.actual_input_shape
        )
    ):
        raise ModelConfigurationError("actual input shape is invalid")
    if not isinstance(contract.nms_in_model, bool):
        raise ModelConfigurationError("inference nms_in_model flag is invalid")
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not _is_finite_number(value)
        or not 0 <= value <= 1
        for value in (contract.confidence_threshold, contract.iou_threshold)
    ):
        raise ModelConfigurationError("inference thresholds are invalid")
    if not _is_sha256(result.frame_sha256):
        raise ModelConfigurationError("inference frame SHA-256 is invalid")
    if result.frame_sha256.lower() != artifact.sha256.lower():
        raise InvalidInputError("inference frame SHA-256 does not match artifact")
    if not isinstance(result.detections, tuple):
        raise ModelConfigurationError("inference detections must be a tuple")
    detections = tuple(_normalize_detection(item) for item in result.detections)
    if not isinstance(result.stage_durations, tuple):
        raise ModelConfigurationError("stage durations must be a tuple")
    for item in result.stage_durations:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ModelConfigurationError("stage duration is invalid")
        stage, duration = item
        if (
            not isinstance(stage, str)
            or not stage.strip()
            or "\x00" in stage
            or len(stage) > 255
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not _is_finite_number(duration)
            or duration < 0
        ):
            raise ModelConfigurationError("stage duration is invalid")
    return detections


_DETECTION_FIELDS = frozenset(
    {
        "class_id",
        "class_name",
        "confidence",
        "xyxy",
        "spatial_zone",
        "defect_type",
        "severity",
    }
)


def _normalize_detection(value: object) -> Detection:
    if isinstance(value, Detection):
        values: dict[str, object] = {
            "class_id": value.class_id,
            "class_name": value.class_name,
            "confidence": value.confidence,
            "xyxy": value.xyxy,
            "spatial_zone": value.spatial_zone,
            "defect_type": value.defect_type,
            "severity": value.severity,
        }
    elif isinstance(value, dict):
        if set(value) != _DETECTION_FIELDS:
            raise ModelConfigurationError("inference detection fields are incomplete")
        values = value
    else:
        raise ModelConfigurationError("inference detection is not a typed mapping")

    class_id = values["class_id"]
    if not isinstance(class_id, int) or isinstance(class_id, bool) or class_id < 0:
        raise ModelConfigurationError("inference detection class_id is invalid")

    class_name = values["class_name"]
    confidence = values["confidence"]
    spatial_zone = values["spatial_zone"]
    defect_type = values["defect_type"]
    severity = values["severity"]
    if any(
        not isinstance(text_value, str)
        or not text_value.strip()
        or "\x00" in text_value
        or len(text_value) > 255
        for text_value in (class_name, spatial_zone, defect_type, severity)
    ):
        raise ModelConfigurationError("inference detection text is invalid")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not _is_finite_number(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ModelConfigurationError("inference detection confidence is invalid")

    box = values["xyxy"]
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        raise ModelConfigurationError("inference detection bounding box is invalid")
    if any(
        not isinstance(coordinate, (int, float))
        or isinstance(coordinate, bool)
        or not _is_finite_number(coordinate)
        for coordinate in box
    ):
        raise ModelConfigurationError("inference detection bounding box is invalid")
    x1, y1, x2, y2 = (float(coordinate) for coordinate in box)
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        raise ModelConfigurationError("inference detection bounding box is invalid")
    return Detection(
        class_id=class_id,
        class_name=class_name,
        confidence=float(confidence),
        xyxy=(x1, y1, x2, y2),
        spatial_zone=spatial_zone,
        defect_type=defect_type,
        severity=severity,
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _classify_failure(error: Exception) -> FailureKind | None:
    if isinstance(error, StaleLease):
        return None
    if isinstance(error, asyncio.TimeoutError):
        return FailureKind.RETRYABLE_INFRA
    if isinstance(error, ModelConfigurationError):
        return FailureKind.MODEL_CONFIGURATION
    if isinstance(error, (InvalidInputError, UnicodeDecodeError, ValueError)):
        return FailureKind.INVALID_INPUT
    return FailureKind.RETRYABLE_INFRA


def _raise_renewal_issue(state: _RenewalState) -> None:
    if state.error is not None:
        raise state.error
    if state.lost.is_set():
        raise StaleLease("lease renewal was rejected")


__all__ = [
    "GROUP_NAME",
    "INFERENCE_TIMEOUT_SECONDS",
    "LEASE_SECONDS",
    "MAX_ENVELOPE_BYTES",
    "RECOVERY_INTERVAL_SECONDS",
    "RENEW_INTERVAL_SECONDS",
    "SUPPORTED_EVENT_TYPE",
    "SUPPORTED_SCHEMA_VERSION",
    "ArtifactLoaderPort",
    "Detection",
    "InferencePort",
    "InferenceResult",
    "InferenceWorker",
    "InspectionEffectPort",
    "InvalidInputError",
    "LoadedArtifact",
    "ModelConfigurationError",
    "ThreadedInspectionEffectAdapter",
    "ThreadedTaskControlAdapter",
    "WorkerProcessResult",
    "WorkerTaskControlPort",
]
