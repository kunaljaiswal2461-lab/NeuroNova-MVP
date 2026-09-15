"""Safety regressions for compiler-owned direct-query expressions.

The former free-form LLM-to-Polars compatibility executor was intentionally
removed. These tests pin the final AST defense used only after the
deterministic QueryPlan compiler has generated an expression.
"""
from __future__ import annotations

import polars as pl
import pytest
from polars.dataframe.group_by import GroupBy

from app.core.config import POLARS_FUNCTION_SIGNATURES
from agentic_engine.conversational import query_allowlist as allow
from agentic_engine.conversational.query_executor import (
    QueryValidationError,
    validate_expression,
)


def test_every_allowlisted_pl_function_has_an_explicit_signature():
    undocumented = sorted(allow.ALLOWED_PL_FUNCTIONS - set(POLARS_FUNCTION_SIGNATURES))
    assert not undocumented, f"allowlisted pl.* functions missing signatures: {undocumented}"


def test_allowlisted_names_exist_in_the_installed_polars_version():
    frame_names = set(dir(pl.DataFrame)) | set(dir(pl.LazyFrame)) | set(dir(GroupBy))
    expr_names = set(dir(pl.Expr)) | set(dir(pl.Series))

    assert not sorted(allow.DATAFRAME_METHODS - frame_names)
    assert not sorted(allow.DATAFRAME_PROPERTIES - frame_names)
    assert not sorted(allow.AGGREGATIONS - (expr_names | frame_names))
    assert not sorted(allow.ALLOWED_PL_ATTRIBUTES - set(dir(pl)))


def test_compiler_expression_passes_the_final_safety_check():
    expression = (
        "df.lazy().filter(pl.col('budget') == 0).select("
        "[pl.len().alias('count')]).collect()"
    )
    validate_expression(expression, compiler_generated=True)


def test_compiler_only_operations_remain_blocked_for_arbitrary_source():
    with pytest.raises(QueryValidationError, match="compiler-only method"):
        validate_expression("df.lazy().collect()")


@pytest.mark.parametrize("expression", [
    '__import__("os").system("id")',
    'pl.read_csv("/etc/passwd")',
    "df.write_csv('/tmp/x')",
    "df.__class__",
    "df.pipe(pl.read_csv)",
    'pl.col("x").map_elements(abs)',
    "(lambda: 1)()",
])
def test_final_safety_check_rejects_dangerous_constructs(expression):
    with pytest.raises(QueryValidationError):
        validate_expression(expression, compiler_generated=True)


def test_validator_accepts_pl_len_with_no_arguments():
    validate_expression("pl.len()")


def test_validator_rejects_pl_len_with_an_argument():
    with pytest.raises(
        QueryValidationError,
        match=r"pl\.len expected 0 argument\(s\), got 1",
    ):
        validate_expression("pl.len(df)")


def test_validator_accepts_pl_col_with_one_argument():
    validate_expression('pl.col("x")')


def test_validator_rejects_pl_col_with_no_arguments():
    with pytest.raises(
        QueryValidationError,
        match=r"pl\.col expected 1 argument\(s\), got 0",
    ):
        validate_expression("pl.col()")


def test_validator_rejects_unknown_pl_function_calls():
    with pytest.raises(QueryValidationError, match=r"unknown pl\.\* function"):
        validate_expression('pl.not_a_real_function("x")')
