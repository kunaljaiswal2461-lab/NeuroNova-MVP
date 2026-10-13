"""SPEC-09 settings: the route mode default, the pass-2 limits, and the dev-only stub script."""
from __future__ import annotations

import pytest

from app.core.config import Settings


def settings(**changes):
    return Settings(_env_file=None, app_env="dev", **changes)


def test_defaults():
    s = settings()
    assert s.nl_sql_route_mode == "hybrid" and s.nl_sql_pass2_max_rows == 50
    assert s.nl_sql_pass2_budget_tokens == 6_000 and s.nl_sql_pass2_max_output_tokens == 1_200
    assert s.nl_sql_stub_llm_path == ""


def test_route_mode_accepts_only_the_two_values(monkeypatch):
    assert settings(nl_sql_route_mode="typed_only").nl_sql_route_mode == "typed_only"
    with pytest.raises(ValueError):
        settings(nl_sql_route_mode="sql_only")
    monkeypatch.setenv("NL_SQL_ROUTE_MODE", "typed_only")
    assert Settings(_env_file=None, app_env="dev").nl_sql_route_mode == "typed_only"


@pytest.mark.parametrize("value", [0, 201])
def test_pass2_row_cap_is_bounded(value):
    with pytest.raises(ValueError):
        settings(nl_sql_pass2_max_rows=value)


def test_the_stub_script_is_allowed_in_dev_and_refused_in_prod():
    assert settings(nl_sql_stub_llm_path="/tmp/script.jsonl").nl_sql_stub_llm_path == "/tmp/script.jsonl"
    with pytest.raises(ValueError, match="NL_SQL_STUB_LLM_PATH"):
        Settings(
            _env_file=None, app_env="prod", nl_sql_stub_llm_path="/tmp/script.jsonl",
            jwt_secret_key="a-long-random-production-secret-0123456789", api_key="k" * 24,
        )
