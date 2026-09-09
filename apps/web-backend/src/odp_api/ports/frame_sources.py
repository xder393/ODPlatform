"""Typed boundaries for decoded camera frames and selected JPEG envelopes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    """One decoded frame before sampling and JPEG encoding."""

    camera_id: UUID
    session_id: UUID
    sequence: int
    captured_at: datetime
    frame: Any


@dataclass(frozen=True, slots=True)
class FrameEnvelope:
    """The immutable selected-frame payload handed to Artifact Saga."""

    camera_id: UUID
    session_id: UUID
    sequence: int
    captured_at: datetime
    jpeg_bytes: bytes
    sha256: str

    @classmethod
    def from_bytes(
        cls,
        *,
        camera_id: UUID,
        session_id: UUID,
        sequence: int,
        captured_at: datetime,
        jpeg_bytes: bytes,
    ) -> FrameEnvelope:
        content = bytes(jpeg_bytes)
        return cls(
            camera_id=camera_id,
            session_id=session_id,
            sequence=sequence,
            captured_at=captured_at,
            jpeg_bytes=content,
            sha256=sha256(content).hexdigest(),
        )


class FrameSource(Protocol):
    def __aiter__(self) -> AsyncIterator[DecodedFrame]: ...

    def frames(self) -> AsyncIterator[DecodedFrame]: ...

    async def close(self) -> None: ...


class FrameEncoderPort(Protocol):
    def encode(self, frame: Any) -> bytes: ...


Clock = Callable[[], datetime]


__all__ = ["Clock", "DecodedFrame", "FrameEncoderPort", "FrameEnvelope", "FrameSource"]
