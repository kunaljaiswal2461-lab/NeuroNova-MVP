"""Request-size guard and security-headers middleware.

Two BaseHTTPMiddleware classes mounted from ``main.create_app``:

    * :class:`RequestSizeLimitMiddleware` — short-circuits oversized
      requests with a 413 *before* their body is read, so a 10GB upload
      attempt cannot waste disk or memory.
    * :class:`SecurityHeadersMiddleware` — appends standard hardening
      headers on every outbound response. Existing headers are never
      overwritten, so a route that wants to ship a custom
      ``Cache-Control`` (e.g. SSE) keeps full control.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cache-Control": "no-store",
}


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared ``Content-Length`` exceeds ``max_bytes``.

    Runs first in the chain (added LAST in ``main.create_app``). A
    rejected request is dropped at the edge — the body is never read,
    so an attacker cannot stream gigabytes through the upstream parser.
    """

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > self.max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "payload_too_large",
                        "message": (
                            f"request body exceeds {self.max_bytes} bytes "
                            f"(declared {declared})"
                        ),
                        "max_bytes": self.max_bytes,
                    },
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Append baseline security headers to every response.

    Each header is added only if the response did not already set it,
    so per-route overrides (e.g. the SSE chat endpoint's own
    ``Cache-Control: no-cache``) keep precedence.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            if header not in response.headers:
                response.headers[header] = value
        return response


__all__ = ["RequestSizeLimitMiddleware", "SecurityHeadersMiddleware"]
