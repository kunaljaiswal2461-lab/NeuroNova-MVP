import os

# Tests construct Settings directly. With no default for APP_ENV (SPEC-02),
# declare the test environment explicitly, once, for the whole suite.
os.environ.setdefault("APP_ENV", "dev")
os.environ.pop("RENDER", None)


# ── eval harness skip logic (SPEC-03 §15.2) ──────────────────────────────────
# Keeps plain ``pytest -q`` free of Docker, network and model-key requirements.
import pytest  # noqa: E402


def pytest_collection_modifyitems(config, items):
    gates = {
        "live_pg": (
            "NN_LIVE_PG",
            "needs the eval fixture; run: docker compose -f backend/evals/fixture/compose.yml "
            "run --rm runner sh -c 'NN_LIVE_PG=1 pytest -q -m live_pg'",
        ),
        "model_eval": (
            "NN_MODEL_EVAL",
            "calls a real model; run with NN_MODEL_EVAL=1 and STAGE4C_EVALUATION_ACK set "
            "(see backend/evals/README.md)",
        ),
    }
    for item in items:
        for marker, (env_var, hint) in gates.items():
            if marker in item.keywords and os.environ.get(env_var) != "1":
                item.add_marker(pytest.mark.skip(reason=f"{marker}: set {env_var}=1. {hint}"))


# ── SQLite DDL fallback for Postgres-only column defaults (SPEC-05 B0) ───────
# Models declare server_default=text("uuid_generate_v4()") / text("now()"). SQLite rejects an
# unparenthesised function default, so rewrite them (as migration 0001's SQLite branch does).
# Test-only: the production schema is unchanged.
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.schema import CreateColumn  # noqa: E402

_SQLITE_DEFAULT_REWRITES = {
    "DEFAULT uuid_generate_v4()": "DEFAULT (lower(hex(randomblob(16))))",
    "DEFAULT now()": "DEFAULT (CURRENT_TIMESTAMP)",
}


@compiles(CreateColumn, "sqlite")
def _sqlite_create_column(element, compiler, **kw):
    ddl = compiler.visit_create_column(element, **kw)
    for pg_default, sqlite_default in _SQLITE_DEFAULT_REWRITES.items():
        ddl = ddl.replace(pg_default, sqlite_default)
    return ddl
