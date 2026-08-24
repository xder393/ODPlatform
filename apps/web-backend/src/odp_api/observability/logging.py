"""Logging safeguards for credentials that must never appear in access logs."""

import logging
from urllib.parse import urlsplit, urlunsplit


class UvicornAccessSecretFilter(logging.Filter):
    """Remove query strings from Uvicorn access records before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _strip_query(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_strip_query(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _strip_query(value) for key, value in record.args.items()}
        record.__dict__.pop("message", None)
        return True


def configure_uvicorn_access_logging() -> None:
    """Install query sanitization on both Uvicorn HTTP and WebSocket loggers."""
    for logger_name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, UvicornAccessSecretFilter) for item in logger.filters):
            logger.addFilter(UvicornAccessSecretFilter())


def _strip_query(value: object) -> object:
    if not isinstance(value, str) or "?" not in value:
        return value
    parsed = urlsplit(value)
    if not parsed.query:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))
