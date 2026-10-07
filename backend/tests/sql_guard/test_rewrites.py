"""T-U3 (rewrites), T-U4 (idempotence), T-U5 (messages), T-U7 (purity), T-U8 (speed). SPEC-04 §11.3."""
from __future__ import annotations

import socket
import statistics
import time

import pytest

from app.services.sql_guard import validate_sql
from app.services.sql_guard.types import Rejection, ValidatedQuery

from .conftest import ROW_CAP, all_accept_sqls, all_reject_cases, SCOPES

ACCEPT = all_accept_sqls()


def ok(sql, scope, **kw):
    result = validate_sql(sql, scope, row_cap=kw.pop("row_cap", ROW_CAP), **kw)
    assert isinstance(result, ValidatedQuery), result
    return result


# ── T-U3 ────────────────────────────────────────────────────────────────────

def test_unqualified_table_is_schema_qualified(scope):
    r = ok("SELECT count(*) FROM orders", scope)
    assert "shop.orders" in r.sql
    assert r.tables == {("shop", "orders")}


def test_unqualified_mixed_case_folds_like_postgres(scope):
    assert ok("SELECT count(*) FROM ORDERS", scope).tables == {("shop", "orders")}
    assert ok('SELECT count(*) FROM shop."Order Events"', scope).tables == {("shop", "Order Events")}


def test_qualified_table_keeps_alias_and_columns_resolve(scope):
    r = ok("SELECT o.status FROM orders o", scope)
    assert ("shop", "orders", "status") in r.columns


@pytest.mark.parametrize(
    "sql,model_limit,applied,must_contain",
    [
        ("SELECT id FROM shop.orders", None, 501, "LIMIT 501"),
        ("SELECT id FROM shop.orders LIMIT 10", 10, 10, "LIMIT 10"),
        ("SELECT id FROM shop.orders LIMIT 500", 500, 500, "LIMIT 500"),
        ("SELECT id FROM shop.orders LIMIT 501", 501, 501, "LIMIT 501"),
        ("SELECT id FROM shop.orders LIMIT 100000", 100000, 501, "LIMIT 501"),
        ("SELECT id FROM shop.orders LIMIT 10 OFFSET 5", 10, 10, "OFFSET 5"),
        ("SELECT id FROM shop.orders LIMIT 100000 OFFSET 5", 100000, 501, "OFFSET 5"),
        ("SELECT id FROM shop.orders ORDER BY id FETCH FIRST 5 ROWS ONLY", 5, 5, "LIMIT 5"),
        ("SELECT id FROM shop.orders LIMIT ALL", None, 501, "LIMIT 501"),
        ("SELECT id FROM shop.orders LIMIT 0", 0, 0, "LIMIT 0"),
    ],
)
def test_T_U3_limit_rules(sql, model_limit, applied, must_contain, scope):
    r = ok(sql, scope)
    assert r.model_limit == model_limit
    assert r.applied_limit == applied
    assert must_contain in r.sql
    assert "FETCH" not in r.sql


def test_T_U3_union_root_gets_the_limit_on_top(scope):
    r = ok("SELECT id FROM shop.orders UNION ALL SELECT id FROM shop.refunds", scope)
    assert r.sql.rstrip().endswith("LIMIT 501")
    assert r.applied_limit == 501


def test_inner_limit_is_untouched(scope):
    r = ok("SELECT * FROM (SELECT id FROM shop.orders LIMIT 7) t", scope)
    assert "LIMIT 7" in r.sql and r.sql.rstrip().endswith("LIMIT 501")


def test_row_cap_is_a_parameter(scope):
    r = ok("SELECT id FROM shop.orders", scope, row_cap=50)
    assert r.applied_limit == 51
    assert ok("SELECT id FROM shop.orders LIMIT 60", scope, row_cap=50).applied_limit == 51
    assert ok("SELECT id FROM shop.orders LIMIT 50", scope, row_cap=50).applied_limit == 50


def test_fingerprint_is_sha256_of_sql(scope):
    import hashlib

    r = ok("SELECT id FROM shop.orders", scope)
    assert r.fingerprint == hashlib.sha256(r.sql.encode()).hexdigest()
    assert r.source_id == scope.source_id and r.snapshot_id == scope.snapshot_id


def test_functions_are_reported_lowercase(scope):
    r = ok("SELECT COUNT(*), Date_Trunc('month', ordered_at) FROM shop.orders GROUP BY 2", scope)
    assert {"count", "date_trunc"} <= r.functions


# ── T-U4 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cid,sql", ACCEPT, ids=[c for c, _ in ACCEPT])
def test_T_U4_idempotent(cid, sql, scope):
    first = ok(sql, scope)
    second = ok(first.sql, scope)
    assert second.sql == first.sql
    assert second.tables == first.tables and second.columns == first.columns
    assert second.applied_limit == first.applied_limit
    assert second.functions == first.functions


# ── T-U5 ────────────────────────────────────────────────────────────────────

def _longest_shared_run(haystack: str, needle: str, limit: int) -> bool:
    """True if any ``limit``-character window of ``needle`` appears in ``haystack``."""
    return any(needle[i:i + limit] in haystack for i in range(0, max(len(needle) - limit + 1, 0)))


REJECTS = all_reject_cases()


@pytest.mark.parametrize("case", REJECTS, ids=[c["id"] for c in REJECTS])
def test_T_U5_messages_leak_nothing(case, scope):
    chosen = SCOPES[case.get("scope")]() if case.get("scope") else scope
    r = validate_sql(case["sql"], chosen, row_cap=ROW_CAP)
    assert isinstance(r, Rejection)
    assert "$" not in r.message and ";" not in r.message
    assert case["sql"] not in r.message or len(case["sql"]) <= 3
    assert not _longest_shared_run(r.message, case["sql"], 65)
    assert r.identifier is None or len(r.identifier) <= 64
    catalog_names = {t.name for t in chosen.tables} | {f"{t.schema}.{t.name}" for t in chosen.tables}
    catalog_names |= {f"{t.name}.{c.name}" for t in chosen.tables for c in t.columns}
    assert len(r.suggestions) <= 5
    assert set(r.suggestions) <= catalog_names


def test_suggestions_for_a_typo_name_real_tables(scope):
    r = validate_sql("SELECT * FROM shop.order", scope, row_cap=ROW_CAP)
    assert isinstance(r, Rejection) and "shop.orders" in r.suggestions


def test_column_suggestions_include_joined_tables(scope):
    r = validate_sql(
        "SELECT region FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id",
        scope, row_cap=ROW_CAP,
    )
    assert isinstance(r, Rejection)
    assert "customers.region_id" in r.suggestions


def test_long_identifier_is_truncated(scope):
    name = "x" * 300
    r = validate_sql(f"SELECT {name} FROM shop.orders", scope, row_cap=ROW_CAP)
    assert isinstance(r, Rejection) and r.identifier is not None and len(r.identifier) <= 64
    assert name not in r.message


# ── T-U7 ────────────────────────────────────────────────────────────────────

def test_T_U7_deterministic_across_100_runs(scope):
    sample = [sql for _, sql in ACCEPT[::25]]
    first = [validate_sql(s, scope, row_cap=ROW_CAP) for s in sample]
    for _ in range(100):
        assert [validate_sql(s, scope, row_cap=ROW_CAP) for s in sample] == first


def test_T_U7_never_opens_a_socket(scope, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("validation must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    for _, sql in ACCEPT[::10]:
        validate_sql(sql, scope, row_cap=ROW_CAP)
    for case in REJECTS:
        validate_sql(case["sql"], scope, row_cap=ROW_CAP)


# ── T-U8 ────────────────────────────────────────────────────────────────────

def test_T_U8_p95_under_50ms(scope):
    timings = []
    for _, sql in ACCEPT:
        t0 = time.perf_counter()
        validate_sql(sql, scope, row_cap=ROW_CAP)
        timings.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(timings, n=100)[94]
    assert p95 < 50, f"p95 {p95:.1f} ms"


# ── found while probing ─────────────────────────────────────────────────────

def test_comments_never_reach_the_executed_sql(scope):
    """A comment is a channel into the text Postgres runs; none is needed (INV-V1)."""
    r = ok("SELECT 1 /* a */ FROM shop.orders -- x */ ; DROP TABLE t; /*", scope)
    assert "/*" not in r.sql and "--" not in r.sql and "DROP" not in r.sql


def test_quoted_keyword_column_is_still_allowed():
    r = ok('SELECT "user" FROM shop.sessions', SCOPES["user_col"]())
    assert ("shop", "sessions", "user") in r.columns
    r = ok("SELECT s.user FROM shop.sessions s", SCOPES["user_col"]())
    assert ("shop", "sessions", "user") in r.columns
