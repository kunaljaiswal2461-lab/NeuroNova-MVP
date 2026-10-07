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
