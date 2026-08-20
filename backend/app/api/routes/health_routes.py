"""Public health-check endpoint.

Serves two purposes:

1. Satisfies the Dockerfile ``HEALTHCHECK`` (``Dockerfile:58-59``), which
   curls ``/api/v1/health`` — a path that 404s without this module.
2. Acts as the always-public target for the external UptimeRobot monitor
   that pings the deployed Render backend every 5 minutes to keep the
   free-tier instance from spinning down.

This module deliberately breaks from the per-module ``APIRouter(prefix=...)``
convention used elsewhere (e.g. ``auth_routes.py``): it registers two paths,
``/api/v1/health`` and ``/health``, that do not share a common prefix, so no
single ``prefix=`` value would work here.
"""
from datetime import datetime, timezone

from fastapi import APIRouter

from app.core.limiter import limiter

router = APIRouter(tags=["health"])


@router.get("/api/v1/health")
@router.get("/health")
@limiter.exempt
async def health_check():
    """Return a minimal liveness payload. Public, no auth, no DB access."""
    return {
        "success": True,
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
