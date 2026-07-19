import logging
import sys
from typing import Any

import structlog

from app.core.config import get_settings


_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "authorization",
    "password",
    "token",
    "secret",
    "key",
    "cookie",
    "session",
    "openai",
    "database_url",
)
_REDACTED = "[REDACTED]"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(needle in lowered for needle in _SENSITIVE_KEY_SUBSTRINGS)


def redact_sensitive(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Structlog processor — redact secret-bearing keys in-place.

    Must run as the very first processor so downstream processors and
    serialisers never see the original values. Also walks a ``headers``
    sub-dictionary if present, since HTTP middlewares routinely log it.
    """
    for key in list(event_dict.keys()):
        if _is_sensitive_key(key):
            event_dict[key] = _REDACTED
    headers = event_dict.get("headers")
    if isinstance(headers, dict):
        event_dict["headers"] = {
            h: (_REDACTED if _is_sensitive_key(h) else v)
            for h, v in headers.items()
        }
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        redact_sensitive,  # MUST be first — strip secrets before any other processor sees them
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
