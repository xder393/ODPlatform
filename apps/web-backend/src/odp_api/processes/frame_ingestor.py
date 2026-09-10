"""Database-controlled frame-ingestor process orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import UUID

from odp_api.modules.ingestion.artifacts import ArtifactSaga
from odp_api.modules.ingestion.service import (
    IngestionHealthPort,
    IngestionReport,
    IngestionService,
)
from odp_api.ports.frame_sources import FrameSource
from odp_api.ports.inspection_sessions import (
    ClaimedInspectionSession,
    IngestionClaim,
    InspectionSession,
    InspectionSessionPort,
)
from odp_api.processes.common import install_signal_stop_event, load_settings
from odp_api.settings import IngestorSettings

LOGGER = logging.getLogger(__name__)


SourceFactory = Callable[[ClaimedInspectionSession], FrameSource]
SagaFactory = Callable[[InspectionSession], ArtifactSaga]
Clock = Callable[[], datetime]


class _IngestionClaimLost(RuntimeError):
    """The database no longer accepts the task's ingestion claim."""


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
        instance_id: UUID,
        heartbeat_interval_seconds: float = 5.0,
        max_concurrent_sessions: int = 10,
        poll_interval_seconds: float = 1.0,
        clock: Clock | None = None,
    ) -> None:
        if not process_id.strip():
            raise ValueError("process_id is required")
        if (
            isinstance(heartbeat_interval_seconds, bool)
            or not isinstance(heartbeat_interval_seconds, (int, float))
            or not math.isfinite(float(heartbeat_interval_seconds))
            or heartbeat_interval_seconds <= 0
        ):
            raise ValueError("heartbeat interval must be finite and positive")
        if isinstance(max_concurrent_sessions, bool) or not isinstance(max_concurrent_sessions, int) or max_concurrent_sessions < 1:
            raise ValueError("max_concurrent_sessions must be a positive integer")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(float(poll_interval_seconds))
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll interval must be finite and positive")
        self._max_sessions = max_concurrent_sessions
        self._poll_interval = poll_interval_seconds
        self._sessions = sessions
        self._ingestion = ingestion
        self._health = health
        self._source_factory = source_factory
        self._saga_factory = saga_factory
        self._process_id = process_id
        if not isinstance(instance_id, UUID):
            raise TypeError("instance_id must be a UUID")
        self._instance_id = instance_id
        self._heartbeat_interval = heartbeat_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Keep polling independently of long-lived streams, within capacity."""
        stop = stop_event or asyncio.Event()
        if stop.is_set():
            return
        active: dict[IngestionClaim, asyncio.Task] = {}
        try:
            while not stop.is_set():
                for claim, task in tuple(active.items()):
                    if task.done():
                        del active[claim]
                        await self._settle_claim(claim, task)
                stopped = await asyncio.to_thread(
                    self._sessions.stop_candidates,
                    self._instance_id,
                    self._max_sessions,
                )
                for claim in stopped:
                    task = active.pop(claim, None)
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await self._finish_stop(claim)
                await asyncio.to_thread(
                    self._sessions.finalize_expired_stops,
                    self._max_sessions,
                )
                capacity = self._max_sessions - len(active)
                if capacity and not stop.is_set():
                    started = await asyncio.to_thread(
                        self._sessions.claim_available,
                        self._process_id,
                        self._instance_id,
                        capacity,
                    )
                    for claimed in started:
                        active[claimed.claim] = asyncio.create_task(
                            self._run_claimed(claimed, None)
                        )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
        finally:
            await self._shutdown_active(active)

    async def _run_claimed(self, claimed: ClaimedInspectionSession, max_frames):
        return await self._run_session(claimed, max_frames)

    async def run_once(
        self,
        *,
        limit: int = 10,
        max_frames_per_session: int | None = None,
    ) -> IngestorRunReport:
        started = await asyncio.to_thread(
            self._sessions.claim_available,
            self._process_id,
            self._instance_id,
            limit,
        )
        reports: list[IngestionReport] = []
        failed = 0
        active: dict[IngestionClaim, asyncio.Task] = {
            claimed.claim: asyncio.create_task(
                self._run_claimed(claimed, max_frames_per_session)
            )
            for claimed in started
        }
        try:
            await asyncio.gather(*active.values(), return_exceptions=True)
        except asyncio.CancelledError:
            await self._shutdown_active(active)
            raise
        finally:
            for claim, task in tuple(active.items()):
                if not task.done():
                    task.cancel()
                try:
                    result = await task
                except asyncio.CancelledError:
                    await self._release_claim(claim)
                except Exception as error:  # noqa: BLE001 - persisted below
                    failed += 1
                    self._log_failure(claim, error)
                    await self._fail_claim(claim, error)
                else:
                    reports.append(result)
                    await self._release_claim(claim)
                del active[claim]
        stopped = await asyncio.to_thread(
            self._sessions.stop_candidates,
            self._instance_id,
            limit,
        )
        for claim in stopped:
            await self._finish_stop(claim)
        await asyncio.to_thread(self._sessions.finalize_expired_stops, limit)
        return IngestorRunReport(
            started=len(started),
            stopped=len(stopped),
            failed=failed,
            reports=tuple(reports),
        )

    async def _run_session(
        self, claimed: ClaimedInspectionSession, max_frames: int | None
    ) -> IngestionReport:
        session = claimed.session
        source = self._source_factory(claimed)
        ingestion = asyncio.create_task(
            self._ingestion.run(
                source,
                organization_id=session.organization_id,
                health=self._health,
                saga=self._saga_factory(session),
                claim=claimed.claim,
                max_frames=max_frames,
            )
        )
        heartbeat = asyncio.create_task(self._heartbeat_until_stop(claimed))
        try:
            done, _ = await asyncio.wait(
                (ingestion, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ingestion in done:
                return ingestion.result()
            if heartbeat.result() is False:
                raise _IngestionClaimLost("ingestion claim was lost")
            raise _IngestionClaimLost("ingestion claim heartbeat stopped")
        finally:
            for task in (ingestion, heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ingestion, heartbeat, return_exceptions=True)
            await source.close()

    async def _heartbeat_until_stop(
        self, claimed: ClaimedInspectionSession, source: FrameSource | None = None
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            alive = await asyncio.to_thread(
                self._sessions.renew,
                claimed.claim,
            )
            if not alive:
                raise _IngestionClaimLost("ingestion claim was lost")

    async def _settle_claim(self, claim: IngestionClaim, task: asyncio.Task) -> bool:
        """Drain a completed task and fence its terminal database transition."""

        try:
            await task
        except asyncio.CancelledError:
            await self._release_claim(claim)
            return False
        except Exception as error:  # noqa: BLE001 - task errors become durable state
            self._log_failure(claim, error)
            await self._fail_claim(claim, error)
            return True
        else:
            await self._release_claim(claim)
            return False

    async def _shutdown_active(self, active: dict[IngestionClaim, asyncio.Task]) -> None:
        """Cancel and drain sources before releasing their matching leases."""

        for task in active.values():
            if not task.done():
                task.cancel()
        for claim, task in tuple(active.items()):
            try:
                await task
            except asyncio.CancelledError:
                await self._release_claim(claim)
            except Exception as error:  # noqa: BLE001 - task errors become durable state
                self._log_failure(claim, error)
                await self._fail_claim(claim, error)
            else:
                await self._release_claim(claim)
            del active[claim]

    async def _release_claim(self, claim: IngestionClaim) -> None:
        release = getattr(self._sessions, "release", None)
        if release is not None:
            await asyncio.to_thread(release, claim)

    async def _fail_claim(self, claim: IngestionClaim, error: BaseException) -> None:
        fail_claim = getattr(self._sessions, "fail_claim", None)
        if fail_claim is not None:
            await asyncio.to_thread(
                fail_claim,
                claim,
                "INGESTOR_FAILED",
                str(error) or type(error).__name__,
            )

    async def _finish_stop(self, claim: IngestionClaim) -> None:
        finish_stop = getattr(self._sessions, "finish_stop", None)
        if finish_stop is not None:
            await asyncio.to_thread(finish_stop, claim)

    @staticmethod
    def _log_failure(claim: IngestionClaim, error: BaseException) -> None:
        LOGGER.exception(
            "frame-ingestor session failed",
            extra={
                "session_id": str(claim.session_id),
                "error_type": type(error).__name__,
            },
        )


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
