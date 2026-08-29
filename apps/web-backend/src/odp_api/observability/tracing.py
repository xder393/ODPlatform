"""Correlation-ID generation and propagation for request tracing.

The first version traces requests with a correlation ID only: the middleware
generates a UUID when the ``X-Correlation-ID`` header is absent, stores it in a
context variable so business code (and audit entries) can read it, echoes it on
the response, and logs one access line per HTTP request. A full OpenTelemetry
SDK integration is a production follow-up; the header and context variable
naming stay stable so the swap is mechanical.
"""

import contextvars
import logging
import uuid

from starlette.datastructures import Headers, MutableHeaders

CORRELATION_ID_HEADER = "X-Correlation-ID"

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "odp_api_correlation_id", default=None
)

logger = logging.getLogger("odp_api.access")


def get_correlation_id() -> str | None:
    """Return the correlation ID assigned to the current request, if any."""
    return _correlation_id.get()


class CorrelationIdMiddleware:
    """Assign or propagate ``X-Correlation-ID`` and log every HTTP request."""

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        supplied_correlation_id = Headers(scope=scope).get(CORRELATION_ID_HEADER)
        try:
            correlation_id = str(uuid.UUID(supplied_correlation_id)) if supplied_correlation_id else str(uuid.uuid4())
        except ValueError:
            correlation_id = str(uuid.uuid4())
        response_status: list[int | None] = [None]

        async def send_with_correlation(message) -> None:
            if message["type"] == "http.response.start":
                response_status[0] = message["status"]
                _append_header(message, CORRELATION_ID_HEADER, correlation_id)
            elif message["type"] == "websocket.accept":
                message.setdefault("headers", [])
                _append_header(message, CORRELATION_ID_HEADER, correlation_id)
            await send(message)

        token = _correlation_id.set(correlation_id)
        try:
            await self._app(scope, receive, send_with_correlation)
        finally:
            _correlation_id.reset(token)

        if scope["type"] == "http":
            logger.info(
                "%s %s completed with status %s",
                scope.get("method"),
                scope.get("path"),
                response_status[0],
                extra={
                    "correlation_id": correlation_id,
                    "http_method": scope.get("method"),
                    "http_path": scope.get("path"),
                    "http_status": response_status[0],
                },
            )


def _append_header(message, name: str, value: str) -> None:
    headers = MutableHeaders(scope=message)
    if headers.get(name) is None:
        headers.append(name, value)
