"""PostgreSQL-authoritative artifact reconciliation process."""

from __future__ import annotations

import asyncio
import logging

from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.storage.minio import MinioObjectStorage
from odp_api.db import create_engine_and_session
from odp_api.modules.ingestion.reconciler import ArtifactReconciler, ReconcileSummary
from odp_api.processes.common import (
    install_signal_stop_event,
    load_settings,
    run_loop,
    utc_now,
)
from odp_api.settings import ArtifactReconcilerSettings

LOGGER = logging.getLogger(__name__)
RECONCILIATION_INTERVAL_SECONDS = 30.0


class ArtifactReconcilerProcess:
    """Run bounded orphan promotion and retention cleanup passes."""

    def __init__(self, reconciler: ArtifactReconciler, *, limit: int = 100) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self._reconciler = reconciler
        self._limit = limit
        self.last_summary: ReconcileSummary | None = None

    async def run_once(self) -> ReconcileSummary:
        summary = await asyncio.to_thread(
            self._reconciler.run_once, utc_now(), limit=self._limit
        )
        self.last_summary = summary
        return summary

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        await run_loop(
            self.run_once,
            interval_seconds=RECONCILIATION_INTERVAL_SECONDS,
            stop_event=stop_event,
        )


def _build_process(settings: ArtifactReconcilerSettings) -> ArtifactReconcilerProcess:
    if settings.database_url is None:
        raise ValueError("ODP_DATABASE_URL is required")
    engine, sessions = create_engine_and_session(settings.database_url)
    repository = SqlAlchemyTaskControlRepository(sessions)
    storage = MinioObjectStorage(
        settings.minio_endpoint or "",
        settings.minio_access_key or "",
        settings.minio_secret_key or "",
        settings.minio_bucket or "",
        secure=settings.minio_secure,
        region=settings.minio_region,
    )
    # Keep the engine alive through the process lifetime; SQLAlchemy owns the
    # pool and closes it when the interpreter receives SIGTERM.
    _ = engine
    return ArtifactReconcilerProcess(ArtifactReconciler(repository, storage))


def main() -> None:
    settings = load_settings(ArtifactReconcilerSettings)
    process = _build_process(settings)

    async def serve() -> None:
        stop = install_signal_stop_event()
        await process.run(stop)

    asyncio.run(serve())


if __name__ == "__main__":
    main()


__all__ = ["ArtifactReconcilerProcess", "main"]
