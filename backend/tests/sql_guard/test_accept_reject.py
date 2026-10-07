"""T-U1 (accept corpora) and T-U2 (reject cases and coverage). SPEC-04 §11.3."""
from __future__ import annotations

import sqlglot
import pytest

from app.services.sql_guard import validate_sql
from app.services.sql_guard.types import Rejection, RejectionCode, ValidatedQuery

from .conftest import ROW_CAP, accept_corpora, all_accept_sqls, all_reject_cases, SCOPES

# SPEC-04 §8.1 "explicitly not allowed" list. Every class here must be exercised by a reject case.
FORBIDDEN_CLASSES = {
    "Command", "Into", "Lock", "TableSample", "Values", "Array", "Any", "All", "ArrayAgg",
    "Parameter", "Placeholder", "CurrentUser", "SessionUser", "CurrentVersion", "CurrentDatabase",
    "Localtimestamp", "UnixToTime", "Rand", "MD5", "Format", "RegexpReplace", "StartsWith",
    "MakeInterval", "JustifyDays", "WidthBucket", "BitwiseAnd",
    # DML / DDL / transaction / Set / Copy classes
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable", "Grant",
    "Transaction", "Commit", "Set", "Copy",
}

# internal_error cannot be reached by any input; test_internal_error_is_fail_closed covers it.
CODES_WITHOUT_INPUT_CASE = {RejectionCode.INTERNAL_ERROR}

ACCEPT = all_accept_sqls()


def test_accept_corpus_sizes_match_the_spec():
    sizes = {k: len(v) for k, v in accept_corpora().items()}
    # §13.2: golden 135 references + 5 acceptable alternatives, mutants 319, natural 23.
    assert sizes == {"golden": 140, "mutants": 319, "natural": 23}


@pytest.mark.parametrize("cid,sql", ACCEPT, ids=[c for c, _ in ACCEPT])
def test_T_U1_accepts(cid, sql, scope):
    result = validate_sql(sql, scope, row_cap=ROW_CAP)
    assert isinstance(result, ValidatedQuery), f"false reject: {result}"
    in_scope = {(t.schema, t.name) for t in scope.tables}
    assert result.tables and result.tables <= in_scope
    assert {(s, t) for s, t, _ in result.columns} <= result.tables


REJECTS = all_reject_cases()


@pytest.mark.parametrize("case", REJECTS, ids=[c["id"] for c in REJECTS])
def test_T_U2_rejects_with_exact_code(case, scope):
    chosen = SCOPES[case.get("scope")]() if case.get("scope") else scope
    result = validate_sql(case["sql"], chosen, row_cap=ROW_CAP)
    assert isinstance(result, Rejection), f"accepted a query that must be rejected: {result}"
    assert result.code.value == case["code"], result.message


def test_T_U2_every_code_is_covered():
    covered = {RejectionCode(c["code"]) for c in REJECTS}
    missing = set(RejectionCode) - covered - CODES_WITHOUT_INPUT_CASE
    assert not missing, sorted(m.value for m in missing)


def test_T_U2_every_forbidden_class_is_covered():
    seen: set[str] = set()
    for case in REJECTS:
        try:
            trees = sqlglot.parse(case["sql"], read="postgres", error_level=sqlglot.ErrorLevel.RAISE)
        except Exception:
            continue
        for tree in trees:
            if tree is not None:
                seen.update(type(node).__name__ for node in tree.walk())
    missing = FORBIDDEN_CLASSES - seen
    assert not missing, sorted(missing)


def test_internal_error_is_fail_closed(scope, monkeypatch):
    """INV-V6: an unexpected exception becomes Rejection(internal_error), never an exception."""
    from app.services.sql_guard import validator

    def boom(*_a, **_k):
        raise ValueError("secret SELECT text must not leak")

    monkeypatch.setattr(validator, "_check_tables", boom)
    result = validate_sql("SELECT 1 FROM shop.orders", scope, row_cap=ROW_CAP)
    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.INTERNAL_ERROR
    assert "secret" not in result.message
