"""Recovery-aware execution of inference messages.

Redis only supplies at-least-once delivery.  This module joins each delivery
to the PostgreSQL control plane, obtains a fenced lease, runs injected artifact
and inference adapters, and sends the resulting command through the durable
inspection-effect boundary before acknowledging Redis.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from odp_schemas.events import EventEnvelope
from pydantic import ValidationError

from odp_api.modules.tasks.commands import (
    InferenceExecutionContract,
    LeaseClaim,
    PublishInferenceCommand,
)
from odp_api.modules.tasks.consumer import (
    GROUP_NAME,
    MAX_ENVELOPE_BYTES,
    STREAM_NAME,
    RedisInferenceConsumer,
    RedisInferenceMessage,
)
from odp_api.modules.tasks.models import FailureKind, TaskStatus
from odp_api.ports.tasks import AdmissionRejected, StaleLease, TaskExecutionPort

LEASE_SECONDS = 20
RENEW_INTERVAL_SECONDS = 5
INFERENCE_TIMEOUT_SECONDS = 10
RECOVERY_INTERVAL_SECONDS = 2
MAX_ATTEMPTS = 3


class InvalidInputError(ValueError):
    """An artifact or model input cannot be interpreted safely."""


class ModelConfigurationError(RuntimeError):
    """The configured model cannot satisfy the persisted execution contract."""


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    """Small, adapter-neutral artifact value handed to inference."""

    content: bytes
    sha256: str
    object_key: str | None = None


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """Adapter-neutral inference output consumed by the effect service."""

    execution_contract: InferenceExecutionContract
    frame_sha256: str
    detections: tuple[dict[str, object], ...] = ()
    stage_durations: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerProcessResult:
    """Observable result of handling one delivery."""

    outcome: str
    acked: bool
    claim: LeaseClaim | None = None


class TaskLookupPort(Protocol):
    """Tenant-scoped task reload used after Redis delivery."""

    def get_task(self, task_id: UUID, organization_id: UUID) -> object | None: ...


class ArtifactLoaderPort(Protocol):
    """Load one PostgreSQL-referenced artifact without exposing storage details."""

    def load(self, task: object) -> object: ...


class InferencePort(Protocol):
    """Run deterministic Mock or production ONNX inference."""

    def infer(self, artifact: object, task: object) -> object: ...


TERMINAL_STATES = frozenset(
    {
        TaskStatus.SUCCEEDED.value,
        TaskStatus.DEAD_LETTER.value,
        TaskStatus.BLOCKED_COMPATIBILITY.value,
        TaskStatus.SKIPPED_STALE.value,
        TaskStatus.SKIPPED_BACKPRESSURE.value,
    }
)
SUPPORTED_EVENT_TYPE = "vision.inference.requested.v1"
SUPPORTED_SCHEMA_VERSION = 1


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
    """Execute one Redis consumer group's messages with PostgreSQL fencing."""

    def __init__(
        self,
        consumer: RedisInferenceConsumer,
        task_execution: TaskExecutionPort | None = None,
        effects: object | None = None,
        artifact_loader: ArtifactLoaderPort | object | None = None,
        inference: InferencePort | object | None = None,
        *,
        task_repository: TaskLookupPort | object | None = None,
        task_lookup: TaskLookupPort | object | None = None,
        task_execution_port: TaskExecutionPort | None = None,
        execution_port: TaskExecutionPort | None = None,
        task_execution_service: TaskExecutionPort | None = None,
        effect_service: object | None = None,
        inspection_effects: object | None = None,
        inference_port: InferencePort | object | None = None,
        worker_id: str | None = None,
        clock: object | None = None,
        renewal_interval_seconds: float = RENEW_INTERVAL_SECONDS,
        inference_timeout_seconds: float = INFERENCE_TIMEOUT_SECONDS,
        recovery_interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
    ) -> None:
        # The explicit aliases keep process composition readable while letting
        # callers migrate from ``task_execution``/``effects`` terminology.
        self._consumer = consumer
        self._execution = task_execution_port or execution_port or task_execution_service or task_execution
        self._effects = effect_service or inspection_effects or effects
        if self._execution is None:
            raise TypeError("a TaskExecutionPort is required")
        if self._effects is None:
            raise TypeError("an InspectionEffectService is required")
        self._task_repository = task_lookup or task_repository
        self._artifact_loader = artifact_loader
        self._inference = inference_port or inference
        self.worker_id = worker_id or str(uuid4())
        if not self.worker_id.strip():
            raise ValueError("worker_id is required")
        if renewal_interval_seconds <= 0:
            raise ValueError("renewal_interval_seconds must be positive")
        if inference_timeout_seconds <= 0:
            raise ValueError("inference_timeout_seconds must be positive")
        if recovery_interval_seconds <= 0:
            raise ValueError("recovery_interval_seconds must be positive")
        self.renewal_interval_seconds = renewal_interval_seconds
        self.inference_timeout_seconds = inference_timeout_seconds
        self.recovery_interval_seconds = recovery_interval_seconds
        self._clock = clock
        self._active_renewals: set[asyncio.Task[None]] = set()

    async def process(self, message: object) -> WorkerProcessResult:
        """Handle one new or reclaimed message and preserve ACK ordering."""

        message_id, stream, raw_payload = _message_parts(message)
        bounded_payload = raw_payload[:MAX_ENVELOPE_BYTES]
        metadata = _metadata_from_payload(raw_payload, message_id, stream)

        if len(raw_payload) > MAX_ENVELOPE_BYTES:
            return await self._quarantine_or_pending(
                message_id,
                stream,
                bounded_payload,
                metadata,
                error="PAYLOAD_TOO_LARGE",
            )

        try:
            envelope = EventEnvelope.model_validate_json(raw_payload)
        except (ValidationError, ValueError, TypeError, UnicodeDecodeError):
            return await self._quarantine_or_pending(
                message_id,
                stream,
                bounded_payload,
                metadata,
                error="MALFORMED_ENVELOPE",
            )

        metadata = _metadata_from_envelope(envelope)
        if (
            envelope.event_type != SUPPORTED_EVENT_TYPE
            or envelope.schema_version != SUPPORTED_SCHEMA_VERSION
        ):
            return await self._quarantine_or_pending(
                message_id,
                stream,
                bounded_payload,
                metadata,
                error="UNSUPPORTED_SCHEMA",
            )

        task_id = _payload_uuid(envelope.payload.get("task_id"))
        dispatch_seq = envelope.payload.get("dispatch_seq")
        if task_id is None or type(dispatch_seq) is not int or dispatch_seq < 1:
            return await self._quarantine_or_pending(
                message_id,
                stream,
                bounded_payload,
                metadata,
                error="MALFORMED_ENVELOPE",
            )
        metadata = _EnvelopeMetadata(
            envelope.event_id,
            envelope.event_type,
            str(envelope.schema_version),
            envelope.organization_id,
            task_id,
            dispatch_seq,
        )

        task = await self._load_task(task_id, envelope.organization_id)
        if task is None:
            # Redis references are not authoritative.  Retain a delivery for a
            # transient database visibility problem instead of ACKing blind.
            return WorkerProcessResult("PENDING", False)
        if not _tenant_matches(task, envelope.organization_id):
            return WorkerProcessResult("PENDING", False)
        if _is_terminal(task) or _is_stale_dispatch(task, dispatch_seq):
            await self._ack(message_id)
            return WorkerProcessResult("DUPLICATE", True)

        claim = await self._claim(task_id, envelope.organization_id)
        if claim is None:
            # Claim rejection is ambiguous until the authoritative row is
            # reloaded.  Only terminal/stale state permits an ACK; a foreign
            # live lease must remain in the PEL for later recovery.
            current = await self._load_task(task_id, envelope.organization_id)
            if current is not None and (
                _is_terminal(current) or _is_stale_dispatch(current, dispatch_seq)
            ):
                await self._ack(message_id)
                return WorkerProcessResult("DUPLICATE", True)
            return WorkerProcessResult("PENDING", False)
        if (
            not isinstance(claim, LeaseClaim)
            or claim.task_id != task_id
            or claim.organization_id != envelope.organization_id
            or claim.lease_owner != self.worker_id
        ):
            return WorkerProcessResult("PENDING", False)

        renewal = _RenewalState(claim)
        renewal_task = asyncio.create_task(self._renew_loop(renewal))
        self._active_renewals.add(renewal_task)
        try:
            # Give the renewal coroutine one scheduling turn before a very
            # fast injected adapter completes; this also makes startup/stops
            # deterministic in tests.
            await asyncio.sleep(0)
            try:
                await _run_with_deadline(
                    self._run_claimed(renewal, task, envelope),
                    self.inference_timeout_seconds,
                )
            except asyncio.CancelledError:
                # Cancellation before commit is a deliberate crash window:
                # do not turn it into a failure or ACK the PEL entry.
                raise
            except StaleLease:
                return WorkerProcessResult("PENDING", False, claim)
            except Exception as error:  # noqa: BLE001 - classify at the boundary
                failure = _classify_failure(error)
                if failure is None:
                    return WorkerProcessResult("PENDING", False, claim)
                await self._record_failure(renewal.claim, failure, error)
                await self._ack(message_id)
                return WorkerProcessResult("FAILED", True, renewal.claim)

            await self._ack(message_id)
            return WorkerProcessResult("SUCCEEDED", True, renewal.claim)
        finally:
            renewal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal_task
            self._active_renewals.discard(renewal_task)

    async def _run_claimed(
        self,
        renewal: _RenewalState,
        task: object,
        envelope: EventEnvelope,
    ) -> None:
        """Load, infer, and commit one claimed task within the deadline."""

        artifact = await self._load_artifact(task)
        _raise_renewal_issue(renewal)
        result = await self._run_inference(artifact, task)
        _raise_renewal_issue(renewal)
        command = _publish_command(
            claim=renewal.claim,
            result=result,
            artifact=artifact,
            correlation_id=envelope.correlation_id,
            now=self._now(),
        )
        # publish() owns the success transaction, including the no-defect
        # path.  The caller ACKs only after this await returns.
        await _call_method(self._effects, "publish", command)

    async def run_once(self) -> int:
        """Recover stale deliveries, then process new deliveries once."""

        await self._consumer.start()
        recovered = await self._consumer.claim_stale()
        new_messages = await self._consumer.read_new()
        count = 0
        for message in [*recovered, *new_messages]:
            await self.process(message)
            count += 1
        return count

    async def recovery_once(self) -> int:
        """Named alias for callers that schedule only the Worker recovery pass."""

        await self._consumer.start()
        messages = await self._consumer.claim_stale()
        for message in messages:
            await self.process(message)
        return len(messages)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Run the delivery/recovery loop until cancellation or stop signal."""

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
        """Stop active renewal coroutines and close the transport."""

        renewals = tuple(self._active_renewals)
        for task in renewals:
            task.cancel()
        for task in renewals:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        close = getattr(self._consumer, "close", None)
        if close is not None:
            await _maybe_await(close())

    async def _load_task(self, task_id: UUID, organization_id: UUID) -> object | None:
        repository = self._task_repository or self._execution
        for name in ("get_task", "get"):
            method = getattr(repository, name, None)
            if method is None:
                continue
            return await _call_compatible(
                method,
                (
                    (task_id, organization_id),
                    (),
                ),
                keyword_candidates=(
                    {"task_id": task_id, "organization_id": organization_id},
                ),
            )
        raise TypeError("an authoritative task lookup method is required")

    async def _claim(self, task_id: UUID, organization_id: UUID) -> LeaseClaim | None:
        return await _call_method(
            self._execution,
            "claim",
            task_id,
            organization_id,
            self.worker_id,
            self._now(),
        )

    async def _load_artifact(self, task: object) -> LoadedArtifact:
        if self._artifact_loader is None:
            payload = getattr(task, "payload", None)
            if isinstance(payload, Mapping) and isinstance(payload.get("content"), bytes):
                return _normalize_artifact(payload)
            raise RuntimeError("artifact loader is required")
        for name in ("load", "get", "read"):
            method = getattr(self._artifact_loader, name, None)
            if method is None:
                continue
            loaded = await _call_compatible(
                method,
                _artifact_call_candidates(task),
            )
            return _normalize_artifact(loaded)
        raise TypeError("artifact loader must expose load(), get(), or read()")

    async def _run_inference(self, artifact: LoadedArtifact, task: object) -> InferenceResult:
        if self._inference is None:
            raise RuntimeError("inference adapter is required")
        for name in ("infer", "inspect", "run"):
            method = getattr(self._inference, name, None)
            if method is None:
                continue
            value = await _call_compatible(
                method,
                (
                    (artifact, task),
                    (artifact,),
                    (artifact.content, task),
                    (artifact.content,),
                ),
            )
            return _normalize_result(value, artifact)
        raise TypeError("inference adapter must expose infer(), inspect(), or run()")

    async def _record_failure(
        self, claim: LeaseClaim, failure: FailureKind, error: Exception
    ) -> None:
        method = self._execution.record_failure
        detail = _error_detail(error)
        await _call_compatible(
            method,
            (
                (claim, failure, self._now()),
                (claim, failure, detail, self._now()),
            ),
        )

    async def _quarantine_or_pending(
        self,
        message_id: str,
        stream: str,
        raw_payload: bytes,
        metadata: _EnvelopeMetadata,
        *,
        error: str,
    ) -> WorkerProcessResult:
        if metadata.organization_id is None or metadata.task_id is None:
            return WorkerProcessResult("PENDING", False)
        # A malformed/unsupported duplicate may already refer to a terminal
        # or newer dispatch.  It is safe to ACK that reference without asking
        # the compatibility transition to mutate an immutable terminal row.
        try:
            task = await self._load_task(metadata.task_id, metadata.organization_id)
        except TypeError:
            # A dedicated quarantine adapter can operate without a separate
            # task lookup; the durable adapter still enforces its own scope.
            task = None
        if task is not None and (
            _is_terminal(task)
            or (
                metadata.dispatch_seq is not None
                and _is_stale_dispatch(task, metadata.dispatch_seq)
            )
        ):
            await self._ack(message_id)
            return WorkerProcessResult("DUPLICATE", True)
        quarantine = _first_method(
            self._execution,
            self._task_repository,
            self._effects,
            names=("quarantine_message", "quarantine"),
        )
        if quarantine is None:
            return WorkerProcessResult("PENDING", False)
        try:
            result = await _call_compatible(
                quarantine,
                (
                    (
                        stream,
                        message_id,
                        metadata.event_id,
                        metadata.event_type,
                        metadata.schema_version,
                        raw_payload[:MAX_ENVELOPE_BYTES],
                        metadata.task_id,
                        metadata.organization_id,
                        self._now(),
                    ),
                    (
                        stream,
                        message_id,
                        metadata.event_id,
                        metadata.event_type,
                        metadata.schema_version,
                        raw_payload[:MAX_ENVELOPE_BYTES],
                        metadata.task_id,
                        metadata.organization_id,
                        self._now(),
                        error,
                    ),
                ),
            )
        except (AdmissionRejected, StaleLease):
            return WorkerProcessResult("PENDING", False)
        if not bool(getattr(result, "ack_after_commit", True)):
            return WorkerProcessResult("QUARANTINED", False)
        await self._ack(message_id)
        return WorkerProcessResult("QUARANTINED", True)

    async def _ack(self, message_id: str) -> None:
        await _call_method(self._consumer, "ack", message_id)

    async def _renew_loop(self, state: _RenewalState) -> None:
        try:
            while True:
                await asyncio.sleep(self.renewal_interval_seconds)
                renewed = await _call_method(
                    self._execution,
                    "renew",
                    state.claim,
                    self._now(),
                )
                if renewed is None:
                    state.lost = asyncio.Event()
                    state.lost.set()
                    return
                state.claim = renewed
        except asyncio.CancelledError:
            raise
        except StaleLease as error:
            state.error = error
            state.lost.set()
        except Exception as error:  # noqa: BLE001 - surfaced to process boundary
            state.error = error
            state.lost.set()

    def _now(self) -> datetime:
        value = self._clock
        if value is None:
            return datetime.now(UTC)
        if callable(value):
            value = value()
        elif hasattr(value, "now"):
            value = value.now()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _run_with_deadline(coroutine: Awaitable[object], seconds: float) -> object:
    """Apply a hard deadline on Python 3.10 and newer runtimes."""

    if getattr(asyncio, "timeout", None) is not None:
        async with asyncio.timeout(seconds):
            return await coroutine
    return await asyncio.wait_for(coroutine, timeout=seconds)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_method(target: object, name: str, *args: object) -> object:
    method = getattr(target, name)
    return await _maybe_await(method(*args))


async def _call_compatible(
    method: object,
    candidates: Sequence[tuple[object, ...]],
    *,
    keyword_candidates: Sequence[Mapping[str, object]] = (),
) -> object:
    signature = None
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        pass
    if signature is not None:
        for args in candidates:
            try:
                signature.bind(*args)
            except TypeError:
                continue
            return await _maybe_await(method(*args))
        for kwargs in keyword_candidates:
            try:
                signature.bind(**kwargs)
            except TypeError:
                continue
            return await _maybe_await(method(**kwargs))
        raise TypeError(f"no compatible call shape for {method!r}")
    last_error: TypeError | None = None
    for args in candidates:
        try:
            return await _maybe_await(method(*args))
        except TypeError as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise TypeError(f"no call shape for {method!r}")


def _first_method(*targets: object | None, names: Sequence[str]) -> object | None:
    for target in targets:
        if target is None:
            continue
        for name in names:
            method = getattr(target, name, None)
            if method is not None:
                return method
    return None


def _message_parts(message: object) -> tuple[str, str, bytes]:
    if isinstance(message, RedisInferenceMessage):
        return message.message_id, message.stream, message.raw_payload
    message_id: object | None = None
    stream = STREAM_NAME
    fields: object | None = None
    if isinstance(message, Mapping):
        message_id = message.get("message_id", message.get("id"))
        stream_value = message.get("stream")
        if stream_value is not None:
            stream = _decode(stream_value)
        fields = message.get("fields")
        if fields is None:
            fields = message
    elif isinstance(message, Sequence) and not isinstance(message, (str, bytes, bytearray)):
        if len(message) != 2:
            raise ValueError("inference message must contain id and fields")
        message_id, fields = message
    else:
        message_id = getattr(message, "message_id", getattr(message, "id", None))
        stream = _decode(getattr(message, "stream", stream))
        fields = getattr(message, "fields", getattr(message, "envelope", None))
    if message_id is None:
        raise ValueError("inference message id is required")
    if isinstance(fields, Mapping):
        payload = fields.get("envelope", fields.get("payload", fields))
    else:
        payload = fields
    return _decode(message_id), stream, _payload_bytes(payload)


def _payload_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="surrogatepass")
    if isinstance(value, EventEnvelope):
        return value.canonical_json().encode("utf-8")
    if isinstance(value, Mapping):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    return str(value).encode("utf-8", errors="replace")


def _metadata_from_payload(raw: bytes, message_id: str, stream: str) -> _EnvelopeMetadata:
    prefix = raw[:MAX_ENVELOPE_BYTES]
    event_id = _regex_uuid(prefix, b"event_id") or uuid5(NAMESPACE_URL, f"{stream}:{message_id}")
    organization_id = _regex_uuid(prefix, b"organization_id")
    task_id = _regex_uuid(prefix, b"task_id")
    event_type = _regex_string(prefix, b"event_type") or SUPPORTED_EVENT_TYPE
    schema_version = _regex_string(prefix, b"schema_version") or "unknown"
    dispatch_seq = _regex_int(prefix, b"dispatch_seq")
    return _EnvelopeMetadata(event_id, event_type, schema_version, organization_id, task_id, dispatch_seq)


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


def _regex_int(raw: bytes, key: bytes) -> int | None:
    pattern = rb'["\']?' + re.escape(key) + rb'["\']?\s*:\s*(-?\d+)'
    match = re.search(pattern, raw)
    if match is None:
        return None
    try:
        return int(match.group(1))
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


def _task_value(task: object, name: str, default: object = None) -> object:
    if isinstance(task, Mapping):
        return task.get(name, default)
    return getattr(task, name, default)


def _tenant_matches(task: object, organization_id: UUID) -> bool:
    task_org = _task_value(task, "organization_id")
    if task_org is None:
        return False
    normalized = _payload_uuid(task_org)
    return normalized == organization_id


def _status(task: object) -> str:
    value = _task_value(task, "status", "")
    return value.value if isinstance(value, TaskStatus) else str(value)


def _is_terminal(task: object) -> bool:
    return _status(task) in TERMINAL_STATES


def _is_stale_dispatch(task: object, dispatch_seq: int) -> bool:
    current = _task_value(task, "dispatch_seq")
    if current is None:
        return False
    try:
        return int(current) != dispatch_seq
    except (TypeError, ValueError):
        return True


def _artifact_call_candidates(task: object) -> tuple[tuple[object, ...], ...]:
    artifact_id = _task_value(task, "artifact_id")
    organization_id = _task_value(task, "organization_id")
    return (
        (task,),
        (artifact_id, organization_id),
        (artifact_id,),
    )


def _normalize_artifact(value: object) -> LoadedArtifact:
    if isinstance(value, LoadedArtifact):
        _verify_digest(value.content, value.sha256)
        return value
    content: object = value
    digest: object | None = None
    object_key: object | None = None
    if isinstance(value, Mapping):
        content = value.get("content", value.get("bytes", value.get("data")))
        digest = value.get("sha256", value.get("digest"))
        object_key = value.get("object_key")
    elif isinstance(value, tuple) and len(value) >= 2:
        content, digest = value[:2]
    else:
        content = getattr(value, "content", getattr(value, "bytes", value))
        digest = getattr(value, "sha256", getattr(value, "digest", None))
        object_key = getattr(value, "object_key", None)
    if not isinstance(content, bytes):
        raise InvalidInputError("artifact content must be bytes")
    actual = hashlib.sha256(content).hexdigest()
    if digest is None:
        digest = actual
    if not isinstance(digest, str) or digest.lower() != actual:
        raise InvalidInputError("artifact SHA-256 does not match content")
    return LoadedArtifact(content, actual, object_key if isinstance(object_key, str) else None)


def _verify_digest(content: bytes, digest: str) -> None:
    if hashlib.sha256(content).hexdigest() != digest.lower():
        raise InvalidInputError("artifact SHA-256 does not match content")


def _normalize_result(value: object, artifact: LoadedArtifact) -> InferenceResult:
    if isinstance(value, InferenceResult):
        if value.frame_sha256 and value.frame_sha256 != artifact.sha256:
            raise InvalidInputError("inference frame SHA-256 does not match artifact")
        return value
    execution = None
    if isinstance(value, Mapping):
        execution = value.get("execution_contract", value.get("execution", value.get("contract")))
        frame_sha = value.get("frame_sha256", value.get("input_frame_sha256", artifact.sha256))
        detections = value.get("detections", ())
        durations = value.get("stage_durations", ())
    else:
        execution = getattr(
            value,
            "execution_contract",
            getattr(value, "execution", getattr(value, "contract", None)),
        )
        frame_sha = getattr(
            value,
            "frame_sha256",
            getattr(value, "input_frame_sha256", artifact.sha256),
        )
        detections = getattr(value, "detections", ())
        durations = getattr(value, "stage_durations", ())
    if frame_sha is None:
        frame_sha = artifact.sha256
    if not isinstance(frame_sha, str) or frame_sha != artifact.sha256:
        raise InvalidInputError("inference frame SHA-256 does not match artifact")
    contract = _normalize_contract(execution, value)
    normalized_detections = tuple(_detection_mapping(item) for item in detections or ())
    normalized_durations = tuple(_duration_pair(item) for item in _duration_items(durations))
    return InferenceResult(contract, frame_sha, normalized_detections, normalized_durations)


def _normalize_contract(value: object, result: object) -> InferenceExecutionContract:
    if isinstance(value, InferenceExecutionContract):
        return value
    source: Mapping[str, object] | None = value if isinstance(value, Mapping) else None
    if source is None and value is not None:
        source = {
            name: getattr(value, name)
            for name in (
                "model_release",
                "model_sha256",
                "onnxruntime_version",
                "execution_provider",
                "actual_input_shape",
                "preprocessing_version",
                "postprocessing_version",
                "confidence_threshold",
                "iou_threshold",
                "nms_mode",
                "nms_in_model",
                "class_map_version",
            )
            if hasattr(value, name)
        }
    if source is None:
        source = {
            name: getattr(result, name)
            for name in (
                "model_release",
                "model_sha256",
                "onnxruntime_version",
                "execution_provider",
                "actual_input_shape",
                "preprocessing_version",
                "postprocessing_version",
                "confidence_threshold",
                "iou_threshold",
                "nms_mode",
                "nms_in_model",
                "class_map_version",
            )
            if hasattr(result, name)
        }
    defaults: dict[str, object] = {
        "model_release": "unknown",
        "model_sha256": "0" * 64,
        "onnxruntime_version": "unknown",
        "execution_provider": "unknown",
        "actual_input_shape": (1, 3, 0, 0),
        "preprocessing_version": "unknown",
        "postprocessing_version": "unknown",
        "confidence_threshold": 0.0,
        "iou_threshold": 0.0,
        "nms_mode": "unknown",
        "nms_in_model": False,
        "class_map_version": "unknown",
    }
    defaults.update(source)
    try:
        return InferenceExecutionContract(
            str(defaults["model_release"]),
            str(defaults["model_sha256"]),
            str(defaults["onnxruntime_version"]),
            str(defaults["execution_provider"]),
            tuple(int(item) for item in defaults["actual_input_shape"]),
            str(defaults["preprocessing_version"]),
            str(defaults["postprocessing_version"]),
            float(defaults["confidence_threshold"]),
            float(defaults["iou_threshold"]),
            str(defaults["nms_mode"]),
            bool(defaults["nms_in_model"]),
            str(defaults["class_map_version"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ModelConfigurationError("inference execution contract is incomplete") from error


def _detection_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return dict(asdict(value))
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise InvalidInputError("inference detection is not JSON-like")


def _duration_items(value: object) -> Sequence[object]:
    if isinstance(value, Mapping):
        return tuple(value.items())
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _duration_pair(value: object) -> tuple[str, float]:
    if isinstance(value, Mapping):
        if len(value) != 1:
            raise InvalidInputError("stage duration mapping must contain one stage")
        key, duration = next(iter(value.items()))
        return str(key), float(duration)
    if isinstance(value, Sequence) and len(value) == 2:
        return str(value[0]), float(value[1])
    raise InvalidInputError("stage duration must be a pair")


def _publish_command(
    *,
    claim: LeaseClaim,
    result: InferenceResult,
    artifact: LoadedArtifact,
    correlation_id: UUID,
    now: datetime,
) -> PublishInferenceCommand:
    return PublishInferenceCommand(
        claim=claim,
        execution_contract=result.execution_contract,
        frame_sha256=result.frame_sha256 or artifact.sha256,
        detections=result.detections,
        stage_durations=result.stage_durations,
        correlation_id=correlation_id,
        database_completed_at=now,
    )


def _classify_failure(error: Exception) -> FailureKind | None:
    if isinstance(error, StaleLease):
        return None
    if isinstance(error, asyncio.TimeoutError):
        return FailureKind.RETRYABLE_INFRA
    if isinstance(error, ModelConfigurationError):
        return FailureKind.MODEL_CONFIGURATION
    if isinstance(error, (InvalidInputError, UnicodeDecodeError, ValueError)):
        return FailureKind.INVALID_INPUT
    name = type(error).__name__.lower()
    text = str(error).lower()
    if "configuration" in name or "model configuration" in text:
        return FailureKind.MODEL_CONFIGURATION
    if any(token in name for token in ("invalidinput", "decode", "malformed", "corrupt")):
        return FailureKind.INVALID_INPUT
    if "frame sha" in text or "artifact sha" in text:
        return FailureKind.INVALID_INPUT
    return FailureKind.RETRYABLE_INFRA


def _raise_renewal_issue(state: _RenewalState) -> None:
    if state.error is not None:
        raise state.error
    if state.lost.is_set():
        raise StaleLease("lease renewal was rejected")


def _error_detail(error: Exception) -> str:
    text = str(error).strip() or type(error).__name__
    return text[:2048]


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


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
    "InferencePort",
    "InferenceResult",
    "InferenceWorker",
    "InvalidInputError",
    "LoadedArtifact",
    "ModelConfigurationError",
    "TaskLookupPort",
    "WorkerProcessResult",
]
