"""SPEC-09 W0: the preparation refactors (write guard module, cassette run index, instruction-leak check)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agentic_engine.llm_engine.base_llm import ChatResult
from app.services.nl_sql import write_guard
from app.services.nl_sql.envelope import EnvelopeError, parse_envelope
from app.services.nl_sql.leak import contains_instruction_run, shingles
from app.services.nl_sql.prompt import instruction_windows, instructions
from evals.cassette import CassetteLLM, CassetteMiss, _structured_request, request_key
from tests.nl_sql.conftest import GOOD_SQL, StubLLM, envelope, run_pass1

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://nn:pw@app-postgres.nn-eval.test:5432/nn_app",
    "STAGE4C_EVALUATION_ACK": "disposable-synthetic-only",
}


# ── W0.1 ─────────────────────────────────────────────────────────────────────

def test_the_planner_and_pass_1_share_one_write_guard():
    from agentic_engine.conversational import postgres_planner
    from app.services.nl_sql import generate

    assert postgres_planner._is_write_or_ddl_request is write_guard.is_write_or_ddl_request
    assert generate.is_write_or_ddl_request is write_guard.is_write_or_ddl_request


def test_pass_1_no_longer_imports_the_stage_4c_planner():
    for name in ("generate.py", "prompt.py", "envelope.py"):
        tree = ast.parse((ROOT / "app/services/nl_sql" / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "conversational" not in node.module, name


@pytest.mark.parametrize("text,expected", [
    ("delete all orders", True), ("please drop table orders", True), ("update orders set x = 1", True),
    ("how many orders were deleted last week", False), ("show updated customers", False),
])
def test_write_guard_behaviour_is_unchanged(text, expected):
    assert write_guard.is_write_or_ddl_request(text) is expected


# ── W0.2 ─────────────────────────────────────────────────────────────────────

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
CALL = dict(model="gpt-4o", system="SYS", user="USR", json_schema=SCHEMA, schema_name="n", max_tokens=1200, temperature=0.0, seed=7)
REQ = {k: v for k, v in CALL.items() if k != "schema_name"}


def test_run_zero_leaves_every_key_byte_identical():
    assert request_key(_structured_request(**REQ)) == request_key(_structured_request(**REQ, run=0))
    assert "run" not in _structured_request(**REQ, run=0)


def test_a_non_zero_run_changes_the_key_and_runs_differ():
    keys = {request_key(_structured_request(**REQ, run=r)) for r in (0, 1, 2)}
    assert len(keys) == 3


class _Inner:
    def __init__(self):
        self.n = 0

    async def chat_structured(self, **kwargs):
        self.n += 1
        return ChatResult({"n": self.n}, "gpt-4o", 1, 1)


async def test_repeat_three_records_three_samples(tmp_path):
    path = tmp_path / "c.jsonl"
    rec = CassetteLLM(path, "record", _Inner(), env=FIXTURE_ENV)
    got = [(await rec.chat_structured(**CALL, run=r)).content_json for r in range(3)]
    assert got == [{"n": 1}, {"n": 2}, {"n": 3}]
    rep = CassetteLLM(path, "replay")
    assert [(await rep.chat_structured(**CALL, run=r)).content_json for r in range(3)] == got
    assert (await rep.chat_structured(**CALL)).content_json == {"n": 1}            # default run is 0
    with pytest.raises(CassetteMiss):
        await rep.chat_structured(**CALL, run=3)


async def test_generate_sql_forwards_run_only_when_non_zero(synced):
    llm = StubLLM()
    await run_pass1(synced, llm, "how many orders are there")
    await run_pass1(synced, llm, "how many orders are there", run=2)
    assert "run" not in llm.calls[0] and llm.calls[1]["run"] == 2


def test_the_runner_hands_each_repeat_its_index():
    source = (ROOT / "evals/runner.py").read_text()
    assert '"run": i' in source


# ── W0.4 ─────────────────────────────────────────────────────────────────────

def test_canary_and_instruction_leak_are_different_codes():
    leak = instructions().split("\n")[0][:60]
    for text in (leak,):
        with pytest.raises(EnvelopeError) as err:
            parse_envelope(envelope(assumptions=[text]))
        assert err.value.code == "instruction_leak"


def test_a_forty_character_run_in_any_string_field_is_a_leak():
    line = "Relative dates (\"last month\", \"this quarter\", \"yesterday\") are written as literal bounds"
    run = line[:40]
    assert len(run) == 40
    for field in ("assumptions", "tables_used"):
        with pytest.raises(EnvelopeError) as err:
            parse_envelope(envelope(**{field: [f"x {run} y"]}))
        assert err.value.code == "instruction_leak"
    with pytest.raises(EnvelopeError) as err:
        parse_envelope({**envelope(kind="clarification", sql=None, output_intent=None), "clarification": f"{run}?"})
    assert err.value.code == "instruction_leak"


def test_thirty_nine_characters_are_not_a_leak_and_whitespace_is_normalised():
    windows = instruction_windows()
    some = next(w for w in sorted(windows) if w == w.strip() and w.count(' ') >= 3)
    assert not contains_instruction_run(some[:39], windows)
    spaced = some.replace(" ", "   \n ")
    assert contains_instruction_run(spaced, windows)


def test_worked_sql_examples_in_the_prompt_do_not_cause_false_positives():
    sql = "SELECT count(*) FROM sales.orders WHERE ordered_at >= TIMESTAMPTZ '2026-05-01 00:00:00+05:30'"
    assert not contains_instruction_run(sql, instruction_windows())
    env, _ = parse_envelope(envelope(sql=sql))
    assert env.kind == "sql"


def test_ordinary_answers_never_trip_the_check():
    env, _ = parse_envelope(GOOD_SQL)
    assert env.kind == "sql"


async def test_an_instruction_leak_is_handled_like_a_canary_leak(synced):
    leak = instructions()[100:200]
    llm = StubLLM(responses=[envelope(assumptions=[leak])])
    result = await run_pass1(synced, llm, "how many orders are there")
    assert result.envelope is None and result.failure_code == "instruction_leak"
    assert result.llm_call is not None and len(llm.calls) == 1
    assert leak[:40] not in repr(result)


def test_shingles_are_cached_per_prompt_text():
    assert shingles(instructions()) is shingles(instructions())
