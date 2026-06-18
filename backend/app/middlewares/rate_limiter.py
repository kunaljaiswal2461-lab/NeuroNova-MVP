"""Rate limiting via slowapi.

Defines a single ``Limiter`` instance keyed by client IP and a custom
exception handler that returns a JSON body matching the application's
existing error envelope (see ``app.exceptions.handlers``).

All limit expressions are read from ``Settings`` so deployments can
tighten or loosen them without code changes.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings


_settings = get_settings()


# Single shared Limiter. Rate-limit feedback is surfaced to clients via
# the Retry-After header on the 429 response (see
# :func:`rate_limit_exceeded_handler` below); slowapi's
# ``headers_enabled=True`` would also emit X-RateLimit-* headers on
# successful responses, but it requires the endpoint to return a
# starlette ``Response`` directly — which would force a wider refactor
# than this change is scoped for.
limiter: Limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[_settings.rate_limit_global],
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a 429 with the app's standard error envelope + Retry-After."""
    # slowapi attaches the matched ``Limit`` instance to the exception.
    # Resolve retry-after from (in order) an explicit attr, the storage
    # window expiry, and finally the bare window length so the header is
    # always populated.
    retry_after = getattr(exc, "retry_after", None)
    limit_obj = getattr(exc, "limit", None)
    if retry_after is None and limit_obj is not None:
        try:
            retry_after = limit_obj.limit.get_expiry()
        except Exception:
            retry_after = None
    limit_str = str(limit_obj.limit) if limit_obj is not None else "rate-limited"

    body = {
        "error": "rate_limit_exceeded",
        "message": f"rate limit exceeded: {limit_str}",
        "retry_after": retry_after,
    }
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(status_code=429, content=body, headers=headers)


__all__ = ["limiter", "rate_limit_exceeded_handler"]
