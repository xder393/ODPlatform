"""Deterministic frame sampling and pre-upload admission policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from numbers import Real

from odp_api.ports.tasks import (
    AdmissionRejected,
    AdmissionRequest,
    AdmissionReservation,
    CameraAdmissionPort,
)

__all__ = [
    "AdmissionDecision",
    "AdmissionPolicy",
    "AdmissionRejected",
    "AdmissionRequest",
    "AdmissionReservation",
    "CameraAdmissionPort",
    "FrameSampler",
]


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """The policy result used by the frame ingestor before object upload."""

    accepted: bool
    reason: str | None = None


class FrameSampler:
    """Accept at most ``target_fps`` monotonically-timestamped frames per second."""

    def __init__(self, target_fps: float) -> None:
        if not isfinite(target_fps) or target_fps <= 0:
            raise ValueError("target_fps must be a finite positive number")
        self._interval_seconds = 1.0 / target_fps
        self._next_due_seconds: float | None = None

    def accept(self, captured_at: datetime | Real) -> bool:
        """Return whether a frame at ``captured_at`` is selected.

        A monotonic schedule is used instead of modulo arithmetic so sampling
        remains deterministic when timestamps are represented by floating
        point values.  Datetimes are converted to UTC epoch seconds by
        ``datetime.timestamp``; callers are expected to provide aware values
        for unambiguous cross-process behavior.
        """

        timestamp = _timestamp_seconds(captured_at)
        if self._next_due_seconds is None:
            self._next_due_seconds = timestamp + self._interval_seconds
            return True
        if timestamp + _EPSILON < self._next_due_seconds:
            return False

        # Advance from the previous schedule, preserving a stable cadence
        # even when a decoder delivers a frame late.
        while self._next_due_seconds <= timestamp + _EPSILON:
            self._next_due_seconds += self._interval_seconds
        return True


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """Reject ordinary frames when they cannot meet the freshness budget."""

    frame_ttl_seconds: float

    def __post_init__(self) -> None:
        if not isfinite(self.frame_ttl_seconds) or self.frame_ttl_seconds <= 0:
            raise ValueError("frame_ttl_seconds must be a finite positive number")

    def evaluate(
        self,
        *,
        ready_count: int,
        oldest_ready_age_seconds: float,
        worker_healthy: bool,
        redis_available: bool,
        frame_age_seconds: float,
    ) -> AdmissionDecision:
        """Evaluate health, freshness, and database-backed backlog signals.

        ``ready_count`` is intentionally accepted as an input for callers that
        already read the camera state row.  A full window is not itself a
        rejection: the repository applies latest-frame-wins by conditionally
        evicting the oldest READY row while holding the camera lock.
        """

        del ready_count  # Capacity is handled atomically by the repository.
        if frame_age_seconds >= self.frame_ttl_seconds:
            return AdmissionDecision(False, "FRAME_TTL_EXHAUSTED")
        if not redis_available:
            return AdmissionDecision(False, "REDIS_UNAVAILABLE")
        if not worker_healthy:
            return AdmissionDecision(False, "WORKER_UNHEALTHY")
        if oldest_ready_age_seconds >= self.frame_ttl_seconds:
            return AdmissionDecision(False, "READY_STALE")
        return AdmissionDecision(True)


def _timestamp_seconds(value: datetime | Real) -> float:
    if isinstance(value, datetime):
        timestamp = value.timestamp()
    elif isinstance(value, Real):
        timestamp = float(value)
    else:
        raise TypeError("captured_at must be a datetime or real number")
    if not isfinite(timestamp):
        raise ValueError("captured_at must be finite")
    return timestamp


_EPSILON = 1e-9
