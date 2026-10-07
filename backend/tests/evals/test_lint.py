"""SPEC-03 §16.2 — linter tests LT1–LT9 (LT8/LT9 logic via an injected runner)."""
from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest
import yaml

from evals.lint import lint_live, lint_static, order_keys_probe_sql, tie_probe_sql
from evals.models import GoldenQuestion, load_meta

META = {
    "fixture_version": 1,
    "reference_clock": "2026-06-15T10:00:00+05:30",
    "reference_time_zone": "Asia/Kolkata",
    "tags": ["aggregate", "ambiguous", "write_attempt", "adversarial"],
    "output_types": ["kpi", "table"],
}
GOOD = {
    "id": "agg-001",
    "question": "How many orders?",
    "tags": ["aggregate"],
    "expect": {"kind": "answer", "reference_sql": "SELECT count(*) FROM shop.orders", "output_type": "kpi"},
    "reviewed_by": "siddhant",
}


def build(tmp_path: Path, questions: list[dict], *, meta=META, manifest_version=1, adversarial=()) -> tuple[Path, Path]:
    n = 0
    while (tmp_path / f"case{n}").exists():
        n += 1
    tmp_path = tmp_path / f"case{n}"
    g = tmp_path / "golden"
    (g / "questions").mkdir(parents=True)
    (g / "adversarial").mkdir()
    (g / "meta.yaml").write_text(yaml.safe_dump(meta))
    (g / "questions" / "a.yaml").write_text(yaml.safe_dump(questions))
    (g / "adversarial" / "seed.yaml").write_text(yaml.safe_dump(list(adversarial)))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"fixture_version": manifest_version}))
    return g, manifest


def rules(tmp_path, questions, **kw) -> set[str]:
    require = kw.pop("require_reviewed", False)
    g, m = build(tmp_path, questions, **kw)
    errors, _ = lint_static(g, m, require_reviewed=require)
    return {e.rule for e in errors}


def variant(**changes) -> dict:
    q = json.loads(json.dumps(GOOD))
    for k, v in changes.items():
        if k.startswith("expect_"):
            q["expect"][k[len("expect_"):]] = v
        else:
            q[k] = v
    return q


def test_clean_set_has_no_errors(tmp_path):
    assert rules(tmp_path, [GOOD], require_reviewed=True) == set()


def test_lt1_schema_and_unique_ids(tmp_path):
    assert rules(tmp_path, [variant(surprise_field=1)]) == {"L1"}
    assert rules(tmp_path, [GOOD, GOOD]) == {"L1"}
    # uniqueness spans the adversarial directory too
    assert rules(tmp_path / "x", [GOOD], adversarial=[variant(tags=["adversarial"])]) == {"L1"}


def test_lt2_vocabulary(tmp_path):
    assert rules(tmp_path, [variant(tags=["not_a_tag"])]) == {"L2"}
    assert rules(tmp_path / "y", [variant(expect_output_type="sparkle")]) == {"L2"}


def test_lt3_shape_per_kind(tmp_path):
    assert rules(tmp_path, [variant(expect_reference_sql=None)]) == {"L3"}
    clar = variant(id="amb-1", tags=["ambiguous"], expect={"kind": "clarification", "reference_sql": "SELECT 1"})
    assert rules(tmp_path / "a", [clar]) == {"L3"}
    ref = variant(id="w-1", tags=["write_attempt"], expect={"kind": "refusal"})
    assert rules(tmp_path / "b", [ref]) == {"L3"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM shop.orders WHERE ordered_at > now() - interval '1 day'",
        "SELECT current_date",
        "SELECT current_timestamp",
        "SELECT localtimestamp",
        "SELECT clock_timestamp()",
    ],
)
def test_lt4_clock_functions(tmp_path, sql):
    assert rules(tmp_path, [variant(expect_reference_sql=sql)]) == {"L4"}


def test_lt4_is_parsed_not_regex(tmp_path):
    sql = "SELECT count(*) FROM shop.orders WHERE status = 'now()' OR channel = 'current_date'"
    assert rules(tmp_path, [variant(expect_reference_sql=sql)]) == set()


def test_lt4_checks_acceptable_answers(tmp_path):
    q = variant(
        id="amb-2",
        tags=["ambiguous"],
        expect={"kind": "clarification", "acceptable_answers": [{"reference_sql": "SELECT now()"}]},
    )
    assert rules(tmp_path, [q]) == {"L4"}


def test_lt5_order_by_required(tmp_path):
    q = variant(expect_order_matters=True)
    assert rules(tmp_path, [q]) == {"L5"}
    ok = variant(expect_order_matters=True, expect_reference_sql="SELECT id FROM shop.orders ORDER BY id")
    assert rules(tmp_path / "ok", [ok]) == set()


def test_lt6_fixture_version_mismatch(tmp_path):
    assert rules(tmp_path, [GOOD], manifest_version=2) == {"L6"}


def test_lt7_require_reviewed(tmp_path):
    q = variant(reviewed_by=None)
    assert rules(tmp_path, [q], require_reviewed=False) == set()
    assert rules(tmp_path / "r", [q], require_reviewed=True) == {"L7"}


# ── live rules, with an injected runner ──────────────────────────────────────

def question(sql: str, *, order_matters: bool) -> GoldenQuestion:
    return GoldenQuestion.model_validate(
        {
            "id": "t-1",
            "question": "q",
            "tags": ["aggregate"],
            "expect": {"kind": "answer", "reference_sql": sql, "order_matters": order_matters},
        }
    )


def test_tie_probe_rewrites_limit_and_exposes_sort_keys():
    probe, n, keys = tie_probe_sql("SELECT name, sum(x) AS spend FROM t GROUP BY name ORDER BY spend DESC LIMIT 5")
    assert n == 5 and keys == [1]                    # alias resolved to the existing column
    assert "LIMIT 6" in probe
    probe, n, keys = tie_probe_sql("SELECT name FROM t ORDER BY created_at DESC, id LIMIT 3")
    assert n == 3 and keys == [1, 2]                 # appended as extra output columns
    assert "__ok0" in probe and "__ok1" in probe
    assert tie_probe_sql("SELECT 1 FROM t ORDER BY 1") is None


def test_lt8_tie_at_limit_cutoff_fails_ll3():
    sql = "SELECT name, n FROM t ORDER BY n DESC LIMIT 2"
    q = question(sql, order_matters=True)

    async def run(statement):
        if "LIMIT 3" in statement:                   # probe: row 2 and row 3 tie on n
            return ["name", "n"], [("a", 9), ("b", 5), ("c", 5)]
        return ["name", "n"], [("a", 9), ("b", 5)]

    errors = asyncio.run(lint_live([q], run))
    assert [e.rule for e in errors] == ["LL3"]

    async def no_tie(statement):
        if "LIMIT 3" in statement:
            return ["name", "n"], [("a", 9), ("b", 5), ("c", 4)]
        return ["name", "n"], [("a", 9), ("b", 5)]

    assert asyncio.run(lint_live([q], no_tie)) == []


def test_lt9_more_than_200_rows_fails_ll4():
    q = question("SELECT id FROM t", order_matters=False)

    async def run(_):
        return ["id"], [(i,) for i in range(201)]

    assert [e.rule for e in asyncio.run(lint_live([q], run))] == ["LL4"]


def test_ll1_ll2_errors_and_nondeterminism():
    q = question("SELECT id FROM t", order_matters=False)

    async def boom(_):
        raise RuntimeError("relation does not exist")

    assert [e.rule for e in asyncio.run(lint_live([q], boom))] == ["LL1"]

    calls = {"n": 0}

    async def flaky(_):
        calls["n"] += 1
        return ["id"], [(calls["n"],)]

    assert [e.rule for e in asyncio.run(lint_live([q], flaky))] == ["LL2"]


def test_ll6_ties_inside_the_result_fail():
    q = question("SELECT name, n FROM t ORDER BY n DESC", order_matters=True)

    async def tied(statement):
        return ["name", "n", "__ok0"], [("a", 9, 9), ("b", 5, 5), ("c", 5, 5)]

    assert [e.rule for e in asyncio.run(lint_live([q], tied))] == ["LL6"]

    async def distinct(statement):
        return ["name", "n", "__ok0"], [("a", 9, 9), ("b", 5, 5), ("c", 4, 4)]

    assert asyncio.run(lint_live([q], distinct)) == []


def test_order_keys_probe_reuses_existing_columns():
    probe, keys = order_keys_probe_sql("SELECT d, count(*) AS n FROM t GROUP BY d ORDER BY n DESC, d")
    assert keys == [1, 0] and "__ok" not in probe
    assert order_keys_probe_sql("SELECT d FROM t") is None
