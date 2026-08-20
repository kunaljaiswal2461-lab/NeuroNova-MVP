"""Tests for the public health-check endpoint.

``Settings`` requires ``API_KEY`` / ``DATABASE_URL``, so seed them via
``setdefault`` *before* importing ``main`` (same pattern as
``backend/e2e_test.py``). No live Postgres is needed — this endpoint never
touches the database.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "health-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routes.health_routes import router as health_router
from app.core.limiter import limiter
from app.middlewares.rate_limiter import rate_limit_exceeded_handler
from main import create_app

HEALTH_PATHS = ["/health", "/api/v1/health"]


@pytest.mark.parametrize("path", HEALTH_PATHS)
async def test_health_returns_ok_without_auth(path):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {"success", "status", "timestamp"}
        assert body["success"] is True
        assert body["status"] == "ok"

        parsed = datetime.fromisoformat(body["timestamp"].replace("Z", "+00:00"))
        assert parsed.utcoffset() == timedelta(0)

        # No auth dependency is attached: even a junk bearer token must
        # still succeed, proving the route never inspects credentials.
        response = await client.get(path, headers={"Authorization": "Bearer not-a-token"})
        assert response.status_code == 200


async def test_health_is_exempt_from_rate_limit():
    """Prove both halves: the limiter was live, and health bypassed it.

    The real app never registers ``SlowAPIMiddleware`` (default limits are
    otherwise dormant — see the plan's "Note: the global limit is not
    currently enforced at all"), so this test builds a throwaway app that
    adds the middleware itself purely to make the exemption observable.
    A non-exempt ``/control`` route is registered on the same app as a
    control: without it, a broken limiter would let health return 200 for
    an unrelated reason and this test would pass vacuously.
    """
    throwaway = FastAPI()
    throwaway.state.limiter = limiter
    throwaway.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    throwaway.add_middleware(SlowAPIMiddleware)
    throwaway.include_router(health_router)

    @throwaway.get("/control")
    async def control():
        return {"ok": True}

    transport = httpx.ASGITransport(app=throwaway)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 210 > the 200/hour default, so a non-exempt route must hit 429.
        health_statuses = {(await client.get("/health")).status_code for _ in range(210)}
        assert health_statuses == {200}

        control_statuses = {(await client.get("/control")).status_code for _ in range(210)}
        assert 429 in control_statuses


def test_dockerfile_healthcheck_path_is_registered():
    dockerfile = Path(__file__).resolve().parents[3] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    match = re.search(r"HEALTHCHECK.*?curl -f http://[^\s/]+(/[^\s]*)", text, re.DOTALL)
    assert match, "could not find a HEALTHCHECK curl URL in the Dockerfile"
    healthcheck_path = match.group(1)

    registered_paths = {route.path for route in create_app().routes}
    assert healthcheck_path in registered_paths
