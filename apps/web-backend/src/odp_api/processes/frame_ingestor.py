"""Database-controlled frame-ingestor process orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import SplitResult, urlsplit, urlunsplit

from odp_api.modules.ingestion.artifacts import ArtifactSaga
from odp_api.modules.ingestion.service import (
    IngestionHealthPort,
    IngestionReport,
    IngestionService,
)
from odp_api.ports.frame_sources import FrameSource
from odp_api.ports.inspection_sessions import InspectionSession, InspectionSessionPort
from odp_api.processes.common import install_signal_stop_event, load_settings
from odp_api.settings import IngestorSettings

LOGGER = logging.getLogger(__name__)


SourceFactory = Callable[[InspectionSession], FrameSource]
SagaFactory = Callable[[InspectionSession], ArtifactSaga]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class IngestorRunReport:
    started: int
    stopped: int
    failed: int
    reports: tuple[IngestionReport, ...]


class FrameIngestor:
    """Claim and run source sessions without an in-memory API control plane."""

    def __init__(
        self,
        sessions: InspectionSessionPort,
        ingestion: IngestionService,
        health: IngestionHealthPort,
        source_factory: SourceFactory,
        saga_factory: SagaFactory,
        *,
        process_id: str,
        heartbeat_interval_seconds: float = 5.0,
        max_concurrent_sessions: int = 10,
        poll_interval_seconds: float = 1.0,
        clock: Clock | None = None,
    ) -> None:
        if not process_id.strip():
            raise ValueError("process_id is required")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        if isinstance(max_concurrent_sessions, bool) or not isinstance(max_concurrent_sessions, int) or max_concurrent_sessions < 1:
            raise ValueError("max_concurrent_sessions must be a positive integer")
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        self._max_sessions = max_concurrent_sessions
        self._poll_interval = poll_interval_seconds
        self._sessions = sessions
        self._ingestion = ingestion
        self._health = health
        self._source_factory = source_factory
        self._saga_factory = saga_factory
        self._process_id = process_id
        self._heartbeat_interval = heartbeat_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Keep polling independently of long-lived streams, within capacity."""
        stop = stop_event or asyncio.Event()
        if stop.is_set():
            return
        active = {}
        try:
            while not stop.is_set():
                for session_id, task in tuple(active.items()):
                    if task.done():
                        await task
                        del active[session_id]
                stopped = await asyncio.to_thread(
                    self._sessions.claim_stop_requests, self._process_id, self._max_sessions
                )
                for session in stopped:
                    task = active.pop(session.session_id, None)
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                capacity = self._max_sessions - len(active)
                if capacity and not stop.is_set():
                    started = await asyncio.to_thread(
                        self._sessions.claim_start_requests, self._process_id, capacity
                    )
                    for session in started:
                        active[session.session_id] = asyncio.create_task(
                            self._run_claimed(session, None)
                        )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
        finally:
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)

    async def _run_claimed(self, session, max_frames):
        try:
            return await self._run_session(session, max_frames)
        except Exception as error:
            LOGGER.exception(
                "frame-ingestor session failed",
                extra={"session_id": str(session.session_id), "error_type": type(error).__name__},
            )
            await asyncio.to_thread(
                self._sessions.mark_failed, session.session_id, self._process_id,
                "INGESTOR_FAILED", str(error) or type(error).__name__, self._clock(),
            )
            return None

    async def run_once(
        self,
        *,
        limit: int = 10,
        max_frames_per_session: int | None = None,
    ) -> IngestorRunReport:
        started = await asyncio.to_thread(
            self._sessions.claim_start_requests, self._process_id, limit
        )
        reports: list[IngestionReport] = []
        failed = 0
        async def run_claimed(session):
            nonlocal failed
            try:
                reports.append(await self._run_session(session, max_frames_per_session))
            except Exception as error:
                failed += 1
                LOGGER.exception(
                    "frame-ingestor session failed",
                    extra={"session_id": str(session.session_id), "error_type": type(error).__name__},
                )
                await asyncio.to_thread(
                    self._sessions.mark_failed,
                    session.session_id,
                    self._process_id,
                    "INGESTOR_FAILED",
                    str(error) or type(error).__name__,
                    self._clock(),
                )
        tasks = [asyncio.create_task(run_claimed(session)) for session in started]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        stopped = await asyncio.to_thread(
            self._sessions.claim_stop_requests, self._process_id, limit
        )
        return IngestorRunReport(
            started=len(started),
            stopped=len(stopped),
            failed=failed,
            reports=tuple(reports),
        )

    async def _run_session(
        self, session: InspectionSession, max_frames: int | None
    ) -> IngestionReport:
        source = self._source_factory(session)
        heartbeat = asyncio.create_task(self._heartbeat_until_stop(session, source))
        try:
            return await self._ingestion.run(
                source,
                organization_id=session.organization_id,
                health=self._health,
                saga=self._saga_factory(session),
                max_frames=max_frames,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await source.close()

    async def _heartbeat_until_stop(self, session: InspectionSession, source: FrameSource) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            alive = await asyncio.to_thread(
                self._sessions.heartbeat,
                session.session_id,
                self._process_id,
                self._clock(),
            )
            if not alive:
                await source.close()
                return


def sanitize_source_uri(uri: str) -> str:
    """Remove URI credentials before a source configuration is persisted."""

    value = uri.strip()
    if not value:
        raise ValueError("source URI is required")
    parsed = urlsplit(value)
    if parsed.scheme in {"rtsp", "rtsps", "http", "https"}:
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("network source URI must include a host")
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        parsed = SplitResult(parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
        return urlunsplit(parsed)
    if parsed.scheme in {"file", ""}:
        return value
    raise ValueError("unsupported source URI scheme")


def _build_process(settings: IngestorSettings) -> FrameIngestor:
    from odp_api.processes.runtime import build_ingestor

    return build_ingestor(settings)


def main() -> None:
    settings = load_settings(IngestorSettings)
    process = _build_process(settings)

    async def serve() -> None:
        stop = install_signal_stop_event()
        await process.run(stop)

    asyncio.run(serve())


if __name__ == "__main__":
    main()


__all__ = ["FrameIngestor", "IngestorRunReport", "main", "sanitize_source_uri"]
