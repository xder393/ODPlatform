"""Logging safeguards for credentials that must never appear in access logs."""

import logging
from urllib.parse import urlsplit, urlunsplit


class UvicornAccessSecretFilter(logging.Filter):
    """Remove query strings only from known Uvicorn request-target arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        target_index = _request_target_index(record)
        if target_index is None or not isinstance(record.args, tuple):
            return True
        request_target = record.args[target_index]
        sanitized_target = _strip_query(request_target)
        if sanitized_target == request_target:
            return True
        sanitized_args = list(record.args)
        sanitized_args[target_index] = sanitized_target
        record.args = tuple(sanitized_args)
        return True


def configure_uvicorn_access_logging() -> None:
    """Install query sanitization on both Uvicorn HTTP and WebSocket loggers."""
    for logger_name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, UvicornAccessSecretFilter) for item in logger.filters):
            logger.addFilter(UvicornAccessSecretFilter())


def _request_target_index(record: logging.LogRecord) -> int | None:
    if not isinstance(record.msg, str) or not isinstance(record.args, tuple):
        return None
    templates = {
        ("uvicorn.access", '%s - "%s %s HTTP/%s" %d'): 2,
        ("uvicorn.error", '%s - "WebSocket %s" [accepted]'): 1,
        ("uvicorn.error", '%s - "WebSocket %s" 403'): 1,
    }
    target_index = templates.get((record.name, record.msg))
    if target_index is None or len(record.args) <= target_index:
        return None
    return target_index


def _strip_query(value: object) -> object:
    if not isinstance(value, str) or "?" not in value:
        return value
    parsed = urlsplit(value)
    if not parsed.query:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))
