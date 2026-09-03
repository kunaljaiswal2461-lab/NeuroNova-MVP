"""Regression tests for NL→Polars compatibility.

Reproduces the two production failures seen against ``movies.csv``:

  * ``DataFrame.sort() got an unexpected keyword argument 'reverse'`` —
    the model emitted a pre-1.0 Polars spelling that the installed
    library rejects at runtime.
  * ``rejected by AST validator: disallowed method/attribute: 'item'`` —
    the validator blocked a legitimate read-only scalar accessor.

And pins the three defences added for them: deterministic legacy-API
normalisation, an allowlist verified against the installed Polars, and a
single LLM repair round driven by the exact failure.
"""
from __future__ import annotations

import polars as pl
import pytest
from polars.expr.datetime import ExprDateTimeNameSpace
from polars.expr.name import ExprNameNameSpace
from polars.expr.string import ExprStringNameSpace
from polars.expr.struct import ExprStructNameSpace
from polars.dataframe.group_by import GroupBy
from polars.expr.whenthen import ChainedThen, ChainedWhen, Then, When

from app.db.models.dataset import FileType
from agentic_engine.conversational import query_allowlist as allow
from agentic_engine.conversational.prompts import query_codegen
from agentic_engine.conversational.query_compat import normalise_expression
from agentic_engine.conversational.query_executor import (
    MAX_CODEGEN_ATTEMPTS,
    QueryValidationError,
    execute_query,
    validate_expression,
)
from agentic_engine.llm_engine.base_llm import LLMUnavailable
from tests.conversational.conftest import FakeStreamingLLM


# ── normalisation ───────────────────────────────────────────────────────────

def test_normalise_rewrites_sort_reverse_to_descending():
    src = 'df.sort("ratio", reverse=True).head(20)'
    out = normalise_expression(src)
    assert out.changed
    assert out.expression == "df.sort('ratio', descending=True).head(20)"
    assert out.rewrites == [".sort(reverse=) -> .sort(descending=)"]


def test_normalise_rewrites_legacy_methods_and_pl_count_preserving_column_name():
    src = 'df.groupby("g").agg(pl.count()).filter(pl.col("count").between(1, 5))'
    out = normalise_expression(src)
    assert out.expression == (
        "df.group_by('g').agg(pl.len().alias('count'))"
        ".filter(pl.col('count').is_between(1, 5))"
    )
    assert set(out.rewrites) == {
        ".groupby -> .group_by",
        "pl.count() -> pl.len().alias('count')",
        ".between -> .is_between",
    }


def test_normalise_is_a_noop_for_current_api_and_ignores_string_contents():
    # Column names that *contain* legacy words must not be touched: the
    # rewrite operates on the AST, not on the source text.
    src = 'df.filter(pl.col("reverse_groupby_between") > 0).sort("x", descending=True)'
    out = normalise_expression(src)
    assert not out.changed
    assert out.expression == src


def test_normalise_passes_syntax_errors_through_unchanged():
    out = normalise_expression("df.sort(")
    assert not out.changed
    assert out.expression == "df.sort("


# ── validator ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expression", [
    'df.select(pl.col("popularity").quantile(0.95)).item()',
    'df.select(pl.col("revenue").sum())[0, 0]',
    'df.filter(pl.col("x").is_between(1, 5))',
    'df.sort("x", descending=True).top_k(5, by="x")',
    "df.shape",
    'df.select(pl.len()).item()',
])
def test_validator_accepts_read_only_polars_1x_constructs(expression):
    validate_expression(expression)


@pytest.mark.parametrize("expression", [
    '__import__("os").system("id")',
    'pl.read_csv("/etc/passwd")',
    'df.write_csv("/tmp/x")',
    "df.to_pandas()",
    "df.__class__",
    "df.pipe(pl.read_csv)",
    'pl.col("x").map_elements(abs)',
    "df.lazy().collect()",
    'df[pl.col("x")]',
    "(lambda: 1)()",
    'df.select(pl.col("x")).item().__class__',
])
def test_validator_still_rejects_dangerous_constructs_after_normalisation(expression):
    with pytest.raises(QueryValidationError):
        validate_expression(normalise_expression(expression).expression)


# ── allowlist ↔ installed Polars ↔ prompt consistency ───────────────────────

def test_every_allowlisted_name_exists_in_installed_polars():
    """The allowlist must never advertise a name the runtime lacks.

    (This is exactly how ``.between`` slipped through before: it was in
    the allowlist and the prompt, but not in Polars 1.x.)
    """
    frame_names = set(dir(pl.DataFrame)) | set(dir(pl.LazyFrame)) | set(dir(GroupBy))
    expr_names = set(dir(pl.Expr)) | set(dir(pl.Series))

    missing = sorted(allow.DATAFRAME_METHODS - frame_names)
    assert not missing, f"DataFrame methods not in Polars: {missing}"

    missing = sorted(allow.DATAFRAME_PROPERTIES - frame_names)
    assert not missing, f"DataFrame properties not in Polars: {missing}"

    missing = sorted(allow.AGGREGATIONS - (expr_names | frame_names))
    assert not missing, f"aggregations not in Polars: {missing}"

    namespace_names = (
        set(dir(ExprNameNameSpace)) | set(dir(ExprStructNameSpace))
        | set(dir(When)) | set(dir(Then))
        | set(dir(ChainedWhen)) | set(dir(ChainedThen))
    )
    missing = sorted(
        allow.EXPRESSION_METHODS - (expr_names | namespace_names | frame_names)
    )
    assert not missing, f"expression methods not in Polars: {missing}"

    missing = sorted(allow.STRING_METHODS - set(dir(ExprStringNameSpace)))
    assert not missing, f".str methods not in Polars: {missing}"

    missing = sorted(allow.DATETIME_METHODS - set(dir(ExprDateTimeNameSpace)))
    assert not missing, f".dt methods not in Polars: {missing}"

    missing = sorted(allow.ALLOWED_PL_ATTRIBUTES - set(dir(pl)))
    assert not missing, f"pl.* names not in Polars: {missing}"

    # Every legacy rename must target an allowlisted current name.
    bad_targets = sorted(
        set(allow.LEGACY_METHOD_RENAMES.values()) - allow.ALLOWED_METHODS
    )
    assert not bad_targets, f"legacy renames target non-allowlisted names: {bad_targets}"


def test_prompt_is_generated_from_the_same_allowlist_the_validator_enforces():
    system = query_codegen.SYSTEM
    for name in sorted(allow.ALLOWED_METHODS | allow.ALLOWED_PL_ATTRIBUTES):
        assert name in system, f"allowlisted {name!r} is not advertised to the model"
    # The two production failures are called out explicitly.
    assert "descending=True" in system
    assert "`.sort(reverse=...)` does not exist" in system
    assert "top_k" in system and "still takes `reverse=`" in system
    assert ".is_between(" in system
    assert "`.between` does not exist" in system
    assert ".item()" in system
    # Untrusted-context guard from the earlier fix is retained.
    assert "untrusted data" in system


# ── end-to-end executor behaviour (no real LLM) ─────────────────────────────

@pytest.mark.asyncio
async def test_screenshot_regression_sort_reverse_now_executes(tiny_csv, tiny_schema, settings_with_key):
    llm = FakeStreamingLLM(json_responses=[{
        "expression": (
            'df.filter(pl.col("price") > 0)'
            '.with_columns((pl.col("revenue") / pl.col("price")).alias("ratio"))'
            '.sort("ratio", reverse=True).head(20)'
        ),
        "explanation": "top rows by revenue-to-price ratio",
    }])

    result = await execute_query(
        "Show me the top 20 rows by revenue-to-price ratio, excluding any where price is zero",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )

    assert result.is_ok, result.error
    assert result.attempts == 1
    assert result.rewrites == [".sort(reverse=) -> .sort(descending=)"]
    assert "descending=True" in result.expression
    assert "reverse" not in result.expression
    assert result.row_count == 5
    ratio_idx = [c.name for c in result.columns].index("ratio")
    ratios = [row[ratio_idx] for row in result.rows]
    assert ratios == sorted(ratios, reverse=True)
    assert len(llm.json_calls) == 1


@pytest.mark.asyncio
async def test_screenshot_regression_item_scalar_is_accepted(tiny_csv, tiny_schema, settings_with_key):
    llm = FakeStreamingLLM(json_responses=[{
        "expression": 'df.select(pl.col("revenue").quantile(0.95)).item()',
        "explanation": "95th percentile of revenue",
    }])

    result = await execute_query(
        "What is the 95th percentile of revenue?",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )

    assert result.is_ok, result.error
    assert [c.name for c in result.columns] == ["result"]
    assert result.row_count == 1
    assert isinstance(result.rows[0][0], float)


@pytest.mark.asyncio
async def test_repair_round_fixes_a_validator_rejection(tiny_csv, tiny_schema, settings_with_key):
    llm = FakeStreamingLLM(json_responses=[
        # First attempt: legitimate intent, forbidden terminal method.
        {"expression": 'df.select(pl.col("revenue").mean()).to_pandas()',
         "explanation": "mean revenue"},
        # Repair: same intent, allowed spelling.
        {"expression": 'df.select(pl.col("revenue").mean()).item()',
         "explanation": "mean revenue"},
    ])

    result = await execute_query(
        "average revenue",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )

    assert result.is_ok, result.error
    assert result.attempts == 2
    assert result.rows == [[pytest.approx(1921.0)]]

    assert len(llm.json_calls) == 2
    repair_prompt = llm.json_calls[1].user
    assert "YOUR PREVIOUS ATTEMPT FAILED" in repair_prompt
    assert "to_pandas" in repair_prompt
    assert "disallowed method/attribute: 'to_pandas'" in repair_prompt
    assert "average revenue" in repair_prompt
    # Repair runs at temperature 0 for determinism.
    assert llm.json_calls[1].temperature == 0.0


@pytest.mark.asyncio
async def test_repair_round_fixes_a_runtime_error(tiny_csv, tiny_schema, settings_with_key):
    llm = FakeStreamingLLM(json_responses=[
        {"expression": 'df.select(pl.col("revenu").sum()).item()',
         "explanation": "typo in column"},
        {"expression": 'df.select(pl.col("revenue").sum()).item()',
         "explanation": "total revenue"},
    ])

    result = await execute_query(
        "total revenue",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )

    assert result.is_ok, result.error
    assert result.attempts == 2
    assert result.rows == [[pytest.approx(9605.0)]]
    assert "execution failed" in llm.json_calls[1].user
    assert "revenu" in llm.json_calls[1].user


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts_with_last_error(tiny_csv, tiny_schema, settings_with_key):
    llm = FakeStreamingLLM(json_responses=[
        {"expression": "df.to_pandas()", "explanation": ""},
        {"expression": "df.to_numpy()", "explanation": ""},
        # Must never be consumed.
        {"expression": "df.head(1)", "explanation": ""},
    ])

    result = await execute_query(
        "anything",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )

    assert not result.is_ok
    assert result.attempts == MAX_CODEGEN_ATTEMPTS == 2
    assert "to_numpy" in (result.error or "")
    assert len(llm.json_calls) == 2
    assert len(llm.json_responses) == 1  # third response untouched


@pytest.mark.asyncio
async def test_repair_llm_outage_reports_the_original_failure(tiny_csv, tiny_schema, settings_with_key):
    llm = FakeStreamingLLM(json_responses=[
        {"expression": "df.to_pandas()", "explanation": ""},
        LLMUnavailable("rate limited"),
    ])

    result = await execute_query(
        "anything",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )

    assert not result.is_ok
    assert result.attempts == 1
    assert "to_pandas" in (result.error or "")


@pytest.mark.asyncio
async def test_dataset_is_loaded_once_across_attempts(tiny_csv, tiny_schema, settings_with_key, monkeypatch):
    from agentic_engine.conversational import query_executor

    calls = {"n": 0}
    real_load = query_executor.load_dataset

    def counting_load(path, file_type):
        calls["n"] += 1
        return real_load(path, file_type)

    monkeypatch.setattr(query_executor, "load_dataset", counting_load)

    llm = FakeStreamingLLM(json_responses=[
        {"expression": 'df.select(pl.col("nope").sum()).item()', "explanation": ""},
        {"expression": 'df.select(pl.col("price").sum()).item()', "explanation": ""},
    ])
    result = await execute_query(
        "total price",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )
    assert result.is_ok, result.error
    assert result.attempts == 2
    assert calls["n"] == 1


# ── review follow-ups: keyword direction, resource guards, render shapes ────

def test_keyword_renames_point_at_the_keyword_the_installed_polars_accepts():
    """Every ``old -> new`` keyword rename must target a real keyword.

    ``DataFrame.top_k`` still uses ``reverse=`` in Polars 1.x, so a blanket
    ``reverse -> descending`` rewrite there would turn *correct* model
    output into deprecated code. This test pins the direction per method.
    """
    import inspect

    for method, mapping in allow.LEGACY_KEYWORD_RENAMES.items():
        owners = [
            getattr(cls, method)
            for cls in (pl.DataFrame, pl.Expr)
            if hasattr(cls, method)
        ]
        assert owners, f"{method} is not a DataFrame or Expr method"
        for old, new in mapping.items():
            for fn in owners:
                params = inspect.signature(fn).parameters
                assert new in params, f"{method}({new}=) does not exist"
                assert old not in params, (
                    f"{method}({old}=) is still the current keyword; "
                    "renaming it is wrong"
                )


def test_dataframe_top_k_keeps_reverse_keyword_untouched():
    expression = 'df.top_k(5, by="x", reverse=True)'
    assert normalise_expression(expression).expression == expression
    validate_expression(expression)


@pytest.mark.parametrize("expression", [
    'df.join(df, on="x", how="cross")',
    'df.sample(1000000, with_replacement=True)',
    'pl.col("x").sample(1000000, with_replacement=True)',
])
def test_validator_rejects_memory_amplifying_keyword_values(expression):
    with pytest.raises(QueryValidationError, match="disallowed argument"):
        validate_expression(expression)


@pytest.mark.parametrize("expression", [
    'df.join(df, on="x", how="inner")',
    'df.sample(5, seed=1)',
])
def test_validator_still_accepts_the_safe_variants(expression):
    validate_expression(expression)


@pytest.mark.asyncio
@pytest.mark.parametrize("expression, expected_columns", [
    ("df.shape", ["result"]),                          # tuple of ints
    ("df.columns", ["result"]),                        # list of str
    ('df.select("country", "revenue").head(2).to_dict(as_series=False)',
     ["country", "revenue"]),                          # dict of lists
    ("df.schema", ["key", "value"]),                   # Schema mapping
    ('df.filter(pl.col("revenue") < 0).select(pl.col("revenue").max()).item()',
     ["result"]),                                      # None scalar
    ('df.select(pl.col("revenue").cast(pl.Decimal).sum()).item()',
     ["result"]),                                      # Decimal scalar
])
async def test_render_handles_non_dataframe_result_shapes(
    expression, expected_columns, tiny_csv, tiny_schema, settings_with_key,
):
    llm = FakeStreamingLLM(json_responses=[{"expression": expression, "explanation": "x"}])

    result = await execute_query(
        "shape question",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )

    assert result.is_ok, result.error
    assert result.attempts == 1
    assert [c.name for c in result.columns] == expected_columns
    assert result.row_count >= 1


@pytest.mark.asyncio
async def test_timeout_is_reported_without_a_repair_round(
    tiny_csv, tiny_schema, settings_with_key, monkeypatch,
):
    """A timed-out query must not be retried: the worker thread cannot be
    cancelled, so a second attempt would double the resource burn."""
    from agentic_engine.conversational import query_executor as qe

    monkeypatch.setattr(qe, "QUERY_TIMEOUT_SECONDS", 0.01)

    def slow_eval(compiled, df):
        import time
        time.sleep(0.2)
        return df

    monkeypatch.setattr(qe, "_eval_sandboxed", slow_eval)
    llm = FakeStreamingLLM(json_responses=[
        {"expression": "df.head(1)", "explanation": "x"},
        {"expression": "df.head(1)", "explanation": "should never be asked"},
    ])

    result = await execute_query(
        "slow question",
        raw_path=tiny_csv,
        file_type=FileType.CSV,
        schema_snapshot=tiny_schema,
        llm=llm,
        settings=settings_with_key,
    )

    assert not result.is_ok
    assert "timeout" in result.error
    assert result.attempts == 1
    assert len(llm.json_calls) == 1
