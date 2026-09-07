"""Shared lifecycle helpers for independently deployed P1 processes.

The helpers in this module keep process entrypoints boring and predictable:
settings are parsed once, readiness is fail-closed, and cancellation is
translated into a bounded stop event instead of an abrupt ACK/commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TypeVar

from odp_api.settings import ProcessReadiness, ProcessSettings

LOGGER = logging.getLogger(__name__)
SettingsT = TypeVar("SettingsT", bound=ProcessSettings)


class ProcessNotReady(RuntimeError):
    """Raised when a process cannot safely start consuming work."""


def load_settings(settings_type: type[SettingsT]) -> SettingsT:
    """Load one process's environment and preserve Pydantic fail-closed errors."""

    return settings_type()


def process_id(role: str) -> str:
    """Return a stable, bounded process identity for DB leases and telemetry."""

    safe_role = role.strip()
    if not safe_role:
        raise ValueError("process role is required")
    configured = os.getenv("ODP_PROCESS_ID", "").strip()
    host = socket.gethostname().strip() or "unknown-host"
    identity = configured or f"{safe_role}@{host}"
    return identity[:255]


def require_ready(readiness: ProcessReadiness) -> None:
    """Stop startup before any stream read or DB claim when a dependency is down."""

    if readiness.ready:
        return
    reasons = ", ".join(readiness.reasons) or "unknown dependency"
    raise ProcessNotReady(f"process readiness failed: {reasons}")


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run_loop(
    step: Callable[[], Awaitable[object]],
    *,
    interval_seconds: float,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run a bounded async iteration and drain cancellation cleanly."""

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        await step()
        if stop.is_set():
            break
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


def install_signal_stop_event() -> asyncio.Event:
    """Install SIGTERM/SIGINT handlers that request cooperative shutdown."""

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        signal_number = getattr(signal, name)
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_number, stop.set)
    return stop


__all__ = [
    "ProcessNotReady",
    "install_signal_stop_event",
    "load_settings",
    "process_id",
    "require_ready",
    "run_loop",
    "utc_now",
]
