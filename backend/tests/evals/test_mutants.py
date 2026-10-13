"""Unit tests for mutant generation and the mutants evaluation logic (SPEC-03 §13.3), no database."""
from __future__ import annotations

import asyncio

from evals.candidates.mutants import evaluate_question, generate_mutants
from evals.grader import ResultColumn, ResultSet
from evals.models import GoldenQuestion

COUNT = (
    "SELECT count(*) FROM shop.orders WHERE status = 'PAID' "
    "AND ordered_at >= TIMESTAMPTZ '2026-03-01 00:00:00+05:30' AND ordered_at < TIMESTAMPTZ '2026-04-01 00:00:00+05:30'"
)
TOP = (
    "SELECT c.full_name, sum(o.total_amount) AS spend FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id "
    "WHERE o.status = 'PAID' GROUP BY c.id, c.full_name ORDER BY spend DESC LIMIT 5"
)


def kinds(sql, order_matters, group=None):
    return [m.kind for m in generate_mutants(sql, order_matters=order_matters) if group in (None, m.group)]


def test_unordered_count_gets_every_applicable_mutation():
    ks = kinds(COUNT, False)
    assert {"rename_aliases", "wrap_cte", "shuffle_rows"} <= set(ks)           # equivalents
    assert {"drop_predicate", "flip_comparison", "agg_swap", "string_case"} <= set(ks)
    assert "flip_order" not in ks and "limit_minus_one" not in ks


def test_ordered_query_gets_order_mutants_and_no_row_shuffle_or_cte():
    ks = kinds(TOP, True)
    assert {"flip_order", "limit_minus_one", "join_flip"} <= set(ks)
    assert "shuffle_rows" not in ks and "wrap_cte" not in ks
    assert "reorder_columns" in ks and "rename_aliases" in ks


def test_rename_updates_order_by_references():
    sqls = [m.sql for m in generate_mutants(TOP, order_matters=True) if m.kind == "rename_aliases"]
    assert len(sqls) == 1
    assert "ORDER BY m2 DESC" in sqls[0] and "spend" not in sqls[0]


def test_ordinals_block_rewrites_that_would_break_them():
    sql = 'SELECT "Event Type", count(*) FROM shop."Order Events" GROUP BY 1 ORDER BY 2 DESC'
    ks = kinds(sql, True)
    assert "reorder_columns" not in ks and "rename_aliases" not in ks
    assert "flip_order" in ks


def test_extra_column_only_for_plain_row_queries():
    plain = "SELECT id, status FROM shop.orders WHERE customer_id IS NULL"
    assert "add_extra_column" in kinds(plain, False)
    assert "add_extra_column" not in kinds(COUNT, False)             # aggregate
    assert "add_extra_column" not in kinds("SELECT DISTINCT status FROM shop.orders", False)


def test_drop_predicate_removes_exactly_one_conjunct():
    dropped = [m.sql for m in generate_mutants(COUNT, order_matters=False) if m.kind == "drop_predicate"]
    assert len(dropped) == 3
    assert any("status" not in s for s in dropped)
    assert all(s.count(" AND ") == 1 for s in dropped)


def test_comparison_flips_and_string_case():
    flips = [m.detail for m in generate_mutants(COUNT, order_matters=False) if m.kind == "flip_comparison"]
    assert "GTE -> GT" in flips and "LT -> LTE" in flips
    cases = [m.sql for m in generate_mutants(COUNT, order_matters=False) if m.kind == "string_case"]
    assert any("'paid'" in s for s in cases)


def test_count_star_swaps_to_a_nullable_column():
    swaps = [m.detail for m in generate_mutants(COUNT, order_matters=False) if m.kind == "agg_swap"]
    assert swaps == ["COUNT(*) -> COUNT(customer_id)"]


def test_every_mutant_differs_from_the_original_and_is_unique():
    ms = generate_mutants(TOP, order_matters=True)
    sqls = [m.sql for m in ms if m.sql]
    assert len(sqls) == len(set(sqls))


# ── evaluation logic with a fake executor ────────────────────────────────────

def _question(sql, *, order_matters=False, notes=None) -> GoldenQuestion:
    return GoldenQuestion.model_validate(
        {
            "id": "t-1", "question": "q", "tags": ["aggregate"], "notes": notes,
            "expect": {"kind": "answer", "reference_sql": sql, "order_matters": order_matters},
        }
    )


def _rs(rows) -> ResultSet:
    return ResultSet((ResultColumn("n", "int8"),), tuple((r,) for r in rows))


def test_survivors_are_classified_and_acceptable_ones_excluded():
    q = _question("SELECT count(*) FROM shop.orders WHERE status = 'PAID' AND channel = 'web'")
    ref = _rs([10])

    async def run(sql):
        # Dropping `status` changes the answer (killed); everything else returns the same rows.
        return _rs([99]) if "status" not in sql else _rs([10])

    results = asyncio.run(evaluate_question(q, ref, run))
    wrong = [r for r in results if r.group == "wrong"]
    assert any(r.status == "fail" for r in wrong)
    survivors = [r for r in wrong if r.status == "pass"]
    assert survivors and all(r.survivor_class == "data_insensitive" for r in survivors)

    accepted_notes = " ".join(f"accept-survivor:{r.kind}" for r in survivors)
    q2 = _question(q.expect.reference_sql, notes=accepted_notes)
    results2 = asyncio.run(evaluate_question(q2, ref, run))
    assert all(r.survivor_class == "accepted" for r in results2 if r.group == "wrong" and r.status == "pass")


def test_lenient_grader_is_distinguished_from_insensitive_data():
    q = _question("SELECT count(*) FROM shop.orders WHERE status = 'PAID'")
    ref = _rs([10])

    async def run(_sql):
        return ResultSet((ResultColumn("n", "int8"),), ((10,),))       # identical rows: data-insensitive

    results = asyncio.run(evaluate_question(q, ref, run))
    assert all(r.survivor_class == "data_insensitive" for r in results if r.status == "pass" and r.group == "wrong")


def test_invalid_mutants_are_reported_not_counted():
    q = _question("SELECT count(*) FROM shop.orders WHERE status = 'PAID'")

    async def run(_sql):
        raise RuntimeError("syntax error")

    results = asyncio.run(evaluate_question(q, _rs([10]), run))
    assert any(r.status == "invalid" for r in results)
