"""Admission-before-encoding application service for realtime frame ingestion."""

from __future__ import annotations

import asyncio
import inspect
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from odp_api.modules.ingestion.artifacts import (
    ArtifactHealth,
    ArtifactSaga,
    SelectedFrame,
)
from odp_api.modules.tasks.admission import AdmissionPolicy, FrameSampler
from odp_api.ports.frame_sources import FrameEncoderPort, FrameEnvelope, FrameSource
from odp_api.ports.tasks import AdmissionRejected, TaskRecord


class IngestionHealthPort(Protocol):
    def snapshot(
        self, organization_id: UUID, camera_id: UUID,
    ) -> ArtifactHealth | Awaitable[ArtifactHealth]: ...


Clock = Callable[[], datetime]


@dataclass(slots=True)
class IngestionReport:
    """Operational counters separated by decode, sampling, admission, and upload."""

    decoded: int = 0
    sampled: int = 0
    encoded: int = 0
    admitted: int = 0
    rejected: int = 0
    rejection_reasons: Counter[str] = field(default_factory=Counter)
    tasks: list[TaskRecord] = field(default_factory=list)


class IngestionService:
    """Consume decoded frames while preserving the no-upload-on-rejection rule."""

    def __init__(
        self,
        encoder: FrameEncoderPort,
        *,
        sample_fps: float = 2.0,
        policy: AdmissionPolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._encoder = encoder
        # A sampler is per source session; sharing state across cameras would
        # make the first frame of a later session depend on another camera.
        self._sample_fps = sample_fps
        self._policy = policy or AdmissionPolicy(frame_ttl_seconds=2)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(
        self,
        source: FrameSource,
        *,
        organization_id: UUID,
        health: IngestionHealthPort,
        saga: ArtifactSaga,
        max_frames: int | None = None,
        content_type: str = "image/jpeg",
    ) -> IngestionReport:
        """Process at most ``max_frames`` decoded frames from one source."""

        if max_frames is not None and max_frames < 1:
            raise ValueError("max_frames must be positive when supplied")
        report = IngestionReport()
        sampler = FrameSampler(self._sample_fps)
        async for decoded in source.frames():
            report.decoded += 1
            if not sampler.accept(decoded.captured_at):
                if max_frames is not None and report.decoded >= max_frames:
                    break
                continue
            report.sampled += 1
            now = self._clock()
            snapshot = health.snapshot(organization_id, decoded.camera_id)
            if inspect.isawaitable(snapshot):
                snapshot = await snapshot
            decision = self._policy.evaluate(
                ready_count=snapshot.ready_count,
                oldest_ready_age_seconds=snapshot.oldest_ready_age_seconds,
                worker_healthy=snapshot.worker_healthy,
                redis_available=snapshot.redis_available,
                frame_age_seconds=max(0.0, (now - _utc(decoded.captured_at)).total_seconds()),
            )
            if not decision.accepted:
                report.rejected += 1
                report.rejection_reasons[decision.reason or "FRAME_REJECTED"] += 1
                if max_frames is not None and report.decoded >= max_frames:
                    break
                continue

            jpeg_bytes = await asyncio.to_thread(self._encoder.encode, decoded.frame)
            report.encoded += 1
            envelope = FrameEnvelope.from_bytes(
                camera_id=decoded.camera_id,
                session_id=decoded.session_id,
                sequence=decoded.sequence,
                captured_at=decoded.captured_at,
                jpeg_bytes=jpeg_bytes,
            )
            selected = SelectedFrame(
                organization_id=organization_id,
                camera_id=decoded.camera_id,
                stream_session_id=decoded.session_id,
                frame_sequence=decoded.sequence,
                captured_at=decoded.captured_at,
                content=envelope.jpeg_bytes,
                correlation_id=decoded.session_id,
                content_sha256=envelope.sha256,
                content_type=content_type,
            )
            outcome = await asyncio.to_thread(saga.ingest, selected, snapshot, now)
            if isinstance(outcome, AdmissionRejected):
                report.rejected += 1
                report.rejection_reasons[outcome.reason] += 1
            else:
                report.admitted += 1
                report.tasks.append(outcome)
            if max_frames is not None and report.decoded >= max_frames:
                break
        return report


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["IngestionHealthPort", "IngestionReport", "IngestionService"]
