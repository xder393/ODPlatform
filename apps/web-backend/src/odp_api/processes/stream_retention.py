"""Independent cooperative process loop for explicit Stream retention policies."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from odp_api.modules.tasks.retention import TrimSummary
from odp_api.processes.common import install_signal_stop_event, load_settings
from odp_api.settings import StreamRetentionSettings

LOGGER = logging.getLogger(__name__)
DEFAULT_RETENTION_INTERVAL_SECONDS = 30.0


class RetentionControllerPort(Protocol):
    async def run_once(self) -> TrimSummary: ...


@dataclass(frozen=True, slots=True)
class RetentionFailure:
    stream_name: str
    error_type: str
    error_message: str


@dataclass(frozen=True, slots=True)
class RetentionProcessSummary:
    streams: tuple[TrimSummary, ...]
    failures: tuple[RetentionFailure, ...]


class StreamRetentionProcess:
    """Run independent policies without allowing one stream to stop another."""

    def __init__(
        self,
        controllers: Sequence[RetentionControllerPort],
        *,
        interval_seconds: float = DEFAULT_RETENTION_INTERVAL_SECONDS,
    ) -> None:
        if not controllers:
            raise ValueError("at least one retention controller is required")
        if interval_seconds <= 0:
            raise ValueError("retention interval must be positive")
        self._controllers = tuple(controllers)
        self.interval_seconds = float(interval_seconds)
        self.last_summary: RetentionProcessSummary | None = None

    async def run_once(self) -> RetentionProcessSummary:
        summaries: list[TrimSummary] = []
        failures: list[RetentionFailure] = []
        for index, controller in enumerate(self._controllers):
            try:
                summaries.append(await controller.run_once())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                stream_name = _controller_stream_name(controller, index)
                failures.append(
                    RetentionFailure(
                        stream_name=stream_name,
                        error_type=type(error).__name__,
                        error_message=str(error)[:1024],
                    )
                )
                LOGGER.exception(
                    "stream retention iteration failed",
                    extra={"redis_stream": stream_name},
                )
        summary = RetentionProcessSummary(tuple(summaries), tuple(failures))
        self.last_summary = summary
        return summary

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            await self.run_once()
            if stop.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)


def _controller_stream_name(controller: object, index: int) -> str:
    policy = getattr(controller, "policy", None)
    stream_name = getattr(policy, "stream_name", None)
    return stream_name if isinstance(stream_name, str) else f"controller-{index}"


def _build_process(settings: StreamRetentionSettings) -> tuple[StreamRetentionProcess, object]:
    from odp_api.processes.runtime import build_retention

    return build_retention(settings)


def main() -> None:
    settings = load_settings(StreamRetentionSettings)
    process, redis = _build_process(settings)

    async def serve() -> None:
        stop = install_signal_stop_event()
        try:
            await process.run(stop)
        finally:
            close = getattr(redis, "aclose", None) or getattr(redis, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    import inspect

    asyncio.run(serve())


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_RETENTION_INTERVAL_SECONDS",
    "RetentionFailure",
    "RetentionProcessSummary",
    "StreamRetentionProcess",
    "main",
]
