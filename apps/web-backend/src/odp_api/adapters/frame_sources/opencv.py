"""Recorded, RTSP, and local-camera sources with lazy OpenCV loading."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from odp_api.ports.frame_sources import Clock, DecodedFrame, FrameEncoderPort


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OpenCvJpegEncoder(FrameEncoderPort):
    """Encode only after the caller has selected a frame."""

    def __init__(self, *, quality: int = 90) -> None:
        if not 1 <= quality <= 100:
            raise ValueError("JPEG quality must be between 1 and 100")
        self._quality = quality

    def encode(self, frame: Any) -> bytes:
        if isinstance(frame, bytes):
            return bytes(frame)
        try:
            import cv2
        except ImportError as error:  # pragma: no cover - exercised in runtime image
            raise RuntimeError("opencv-python-headless is required for camera encoding") from error
        success, encoded = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._quality]
        )
        if not success:
            raise ValueError("OpenCV could not encode the selected frame")
        return bytes(encoded.tobytes())


class _OpenCvSource:
    def __init__(
        self,
        source: str | int,
        *,
        camera_id: UUID,
        session_id: UUID,
        clock: Clock | None = None,
        capture_factory: Callable[[str | int], Any] | None = None,
        reconnect_delay_seconds: float = 0.5,
        max_reconnect_delay_seconds: float = 10.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if reconnect_delay_seconds <= 0 or max_reconnect_delay_seconds < reconnect_delay_seconds:
            raise ValueError("reconnect delay bounds are invalid")
        self.source = source
        self.camera_id = camera_id
        self.session_id = session_id
        self._clock = clock or _utc_now
        self._capture_factory = capture_factory or self._default_capture_factory
        self._reconnect_delay_seconds = reconnect_delay_seconds
        self._max_reconnect_delay_seconds = max_reconnect_delay_seconds
        self._sleep = sleep
        self._capture: Any | None = None
        self._sequence = 0
        self._closed = False

    @staticmethod
    def _default_capture_factory(source: str | int) -> Any:
        try:
            import cv2
        except ImportError as error:  # pragma: no cover - exercised in runtime image
            raise RuntimeError("opencv-python-headless is required for frame ingestion") from error
        return cv2.VideoCapture(source)

    def _open(self) -> Any:
        capture = self._capture_factory(self.source)
        if not capture.isOpened():
            release = getattr(capture, "release", None)
            if release is not None:
                release()
            raise OSError(f"frame source could not be opened: {self.source!r}")
        self._capture = capture
        return capture

    async def _read(self) -> tuple[bool, Any]:
        capture = self._capture or self._open()
        return await asyncio.to_thread(capture.read)

    async def _release(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            await asyncio.to_thread(capture.release)

    async def close(self) -> None:
        self._closed = True
        await self._release()

    def __aiter__(self):
        return self.frames()

    def _decoded(self, frame: Any) -> DecodedFrame:
        self._sequence += 1
        return DecodedFrame(
            camera_id=self.camera_id,
            session_id=self.session_id,
            sequence=self._sequence,
            captured_at=self._clock(),
            frame=frame,
        )


class RecordedVideoSource(_OpenCvSource):
    """Loop a recorded source deterministically for demos and replay tests."""

    def __init__(self, path: str, **kwargs: Any) -> None:
        super().__init__(path, **kwargs)
        self.path = path

    async def frames(self):
        while not self._closed:
            try:
                ok, frame = await self._read()
            except OSError:
                await self._release()
                await self._sleep(self._reconnect_delay_seconds)
                continue
            if ok:
                yield self._decoded(frame)
                continue
            await self._release()
            if self._closed:
                break
            # EOF is a clean deterministic loop, not a source failure.
            await asyncio.sleep(0)

    async def restart_and_read_first(self) -> DecodedFrame:
        await self._release()
        ok, frame = await self._read()
        if not ok:
            await self._release()
            raise EOFError("recorded video has no decodable frame")
        return self._decoded(frame)


class RtspSource(_OpenCvSource):
    """Reconnect an RTSP source with capped exponential backoff."""

    async def frames(self):
        delay = self._reconnect_delay_seconds
        while not self._closed:
            try:
                ok, frame = await self._read()
            except OSError:
                ok, frame = False, None
            if ok:
                delay = self._reconnect_delay_seconds
                yield self._decoded(frame)
                continue
            await self._release()
            if self._closed:
                break
            await self._sleep(delay)
            delay = min(self._max_reconnect_delay_seconds, delay * 2)


class LocalCameraSource(_OpenCvSource):
    """Read an explicitly selected local camera device index."""

    def __init__(self, device_index: int, **kwargs: Any) -> None:
        if device_index < 0:
            raise ValueError("camera device index must be non-negative")
        super().__init__(device_index, **kwargs)
        self.device_index = device_index

    async def frames(self):
        delay = self._reconnect_delay_seconds
        while not self._closed:
            try:
                ok, frame = await self._read()
            except OSError:
                ok, frame = False, None
            if ok:
                delay = self._reconnect_delay_seconds
                yield self._decoded(frame)
                continue
            await self._release()
            if self._closed:
                break
            await self._sleep(delay)
            delay = min(self._max_reconnect_delay_seconds, delay * 2)


__all__ = ["LocalCameraSource", "OpenCvJpegEncoder", "RecordedVideoSource", "RtspSource"]
