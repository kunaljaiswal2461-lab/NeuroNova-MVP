"""Host guards (SPEC-03 INV-P0-01).

Harness components refuse to run unless the application DB and the client DB
are the fixture's network aliases. There is deliberately no override flag.
"""
from __future__ import annotations

import os
from typing import Mapping

from sqlalchemy.engine import make_url

FIXTURE_APP_HOST = "app-postgres.nn-eval.test"
FIXTURE_CLIENT_HOST = "client-postgres.nn-eval.test"
# Resolves to the same server but is NOT in its certificate (test F2: hostname mismatch must fail).
FIXTURE_CLIENT_ALT_HOST = "client-postgres-alt.nn-eval.test"
CLIENT_HOST_ENV = "NN_EVAL_CLIENT_HOST"


class FixtureGuardError(RuntimeError):
    """Raised when a harness component is pointed at anything but the fixture."""


def assert_fixture_hosts(database_url: str | None, client_host: str | None = FIXTURE_CLIENT_HOST) -> None:
    if not database_url:
        raise FixtureGuardError("DATABASE_URL is not set; the harness only runs against the fixture app DB")
    try:
        app_host = make_url(database_url).host
    except Exception as exc:  # noqa: BLE001 - never echo the URL (it may carry a password)
        raise FixtureGuardError("DATABASE_URL could not be parsed") from exc
    if app_host != FIXTURE_APP_HOST:
        raise FixtureGuardError(
            f"refusing to run: application DB host is {app_host!r}, expected {FIXTURE_APP_HOST!r}"
        )
    if client_host != FIXTURE_CLIENT_HOST:
        raise FixtureGuardError(
            f"refusing to run: client DB host is {client_host!r}, expected {FIXTURE_CLIENT_HOST!r}"
        )


def assert_fixture_environment(env: Mapping[str, str] | None = None) -> None:
    """Check the process environment the runner container is started with."""
    env = os.environ if env is None else env
    assert_fixture_hosts(env.get("DATABASE_URL"), env.get(CLIENT_HOST_ENV, FIXTURE_CLIENT_HOST))
