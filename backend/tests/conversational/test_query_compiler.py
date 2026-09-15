"""Semantic validation and deterministic compilation tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import polars as pl
import pytest

from agentic_engine.conversational.query_compiler import (
    SemanticValidationError,
    compile_query_plan,
    validate_query_plan,
)
from agentic_engine.conversational.query_plan import QueryPlan
from agentic_engine.profiler.report import (
    ColumnStats,
    HealthSection,
    ProfileReport,
    QualitySection,
    RelationshipSection,
    SchemaColumn,
    SchemaSection,
    SemanticSection,
    StatsSection,
)


@pytest.fixture
def query_profile() -> ProfileReport:
    return ProfileReport(
        dataset_id=uuid.uuid4(),
        generated_at=datetime.now(timezone.utc),
        schema=SchemaSection(
            row_count=4,
            col_count=3,
            columns=[
                SchemaColumn(name="country", dtype="string", polars_dtype="String", nullable=False),
                SchemaColumn(name="revenue", dtype="float", polars_dtype="Float64", nullable=False),
                SchemaColumn(name="budget", dtype="float", polars_dtype="Float64", nullable=False),
            ],
        ),
        stats=StatsSection(columns=[
            ColumnStats(name="country", inferred_kind="categorical"),
            ColumnStats(name="revenue", inferred_kind="numeric"),
            ColumnStats(name="budget", inferred_kind="numeric"),
        ]),
        quality=QualitySection(duplicate_row_pct=0.0, columns=[]),
        relationships=RelationshipSection(correlations=[]),
        semantic=SemanticSection(columns=[]),
        health=HealthSection(score=100.0, grade="A", components={}),
    )


@pytest.fixture
def query_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "country": ["US", "US", "UK", "DE"],
        "revenue": [100.0, 300.0, 50.0, 150.0],
        "budget": [0.0, 100.0, 0.0, 50.0],
    })


def _run(plan_data, profile, frame):
    plan = QueryPlan.model_validate(plan_data)
    return compile_query_plan(plan, profile).execute(frame)


@pytest.mark.parametrize("operator,value,expected_rows", [
    ("equals", "US", 2),
    ("not_equals", "US", 2),
    ("contains", "U", 3),
    ("not_contains", "U", 1),
])
def test_string_filter_operators(operator, value, expected_rows, query_profile, query_frame):
    result = _run({"filters": [{"column": "country", "operator": operator, "value": value}]}, query_profile, query_frame)
    assert result.height == expected_rows


@pytest.mark.parametrize("operator,value,expected_rows", [
    ("greater_than", 50, 3),
    ("less_than", 100, 1),
    ("greater_or_equal", 100, 3),
    ("less_or_equal", 100, 2),
])
def test_numeric_filter_operators(operator, value, expected_rows, query_profile, query_frame):
    result = _run({"filters": [{"column": "revenue", "operator": operator, "value": value}]}, query_profile, query_frame)
    assert result.height == expected_rows


def test_contains_on_numeric_column_returns_a_semantic_error(query_profile):
    plan = QueryPlan.model_validate({"filters": [{"column": "revenue", "operator": "contains", "value": "1"}]})
    with pytest.raises(SemanticValidationError, match="string or categorical"):
        validate_query_plan(plan, query_profile)


@pytest.mark.parametrize("metric_type,expected", [
    ("count", 4),
    ("count_distinct", 3),
    ("mean", 150.0),
    ("sum", 600.0),
    ("min", 50.0),
    ("max", 300.0),
    ("median", 125.0),
])
def test_metrics_compile_to_correct_results(metric_type, expected, query_profile, query_frame):
    metric = {"type": metric_type, "name": "value"}
    if metric_type not in {"count"}:
        metric["column"] = "country" if metric_type == "count_distinct" else "revenue"
    result = _run({"metrics": [metric]}, query_profile, query_frame)
    assert result.item(0, "value") == expected


def test_mean_on_string_column_returns_a_semantic_error(query_profile):
    plan = QueryPlan.model_validate({"metrics": [{"type": "mean", "column": "country", "name": "avg_country"}]})
    with pytest.raises(SemanticValidationError, match="numeric column"):
        validate_query_plan(plan, query_profile)


def test_group_by_count_produces_correct_result(query_profile, query_frame):
    result = _run({
        "group_by": {"columns": ["country"]},
        "metrics": [{"type": "count", "name": "movie_count"}],
        "sort": {"column": "country", "direction": "asc"},
    }, query_profile, query_frame)
    assert result.rows() == [("DE", 1), ("UK", 1), ("US", 2)]


def test_percentage_of_all_rows_is_between_zero_and_one_hundred(query_profile, query_frame):
    result = _run({
        "filters": [{"column": "budget", "operator": "equals", "value": 0}],
        "metrics": [{"type": "percentage_of_all_rows", "name": "zero_budget_pct"}],
    }, query_profile, query_frame)
    assert result.item(0, "zero_budget_pct") == 50.0


def test_percentage_of_group_is_compiled_after_grouping(query_profile, query_frame):
    result = _run({
        "group_by": {"columns": ["country"]},
        "metrics": [{"type": "percentage_of_group", "name": "share"}],
        "sort": {"column": "country", "direction": "asc"},
    }, query_profile, query_frame)
    assert result.rows() == [("DE", 25.0), ("UK", 25.0), ("US", 50.0)]


def test_sort_ascending_on_existing_column(query_profile, query_frame):
    result = _run({
        "select_columns": ["revenue"],
        "sort": {"column": "revenue", "direction": "asc"},
    }, query_profile, query_frame)
    assert result.get_column("revenue").to_list() == [50.0, 100.0, 150.0, 300.0]


def test_sort_on_unknown_column_returns_a_semantic_error(query_profile):
    plan = QueryPlan.model_validate({"sort": {"column": "missing", "direction": "asc"}})
    with pytest.raises(SemanticValidationError, match="sort column does not exist"):
        validate_query_plan(plan, query_profile)


def test_limit_returns_at_most_five_rows(query_profile, query_frame):
    result = _run({"limit": 2}, query_profile, query_frame)
    assert result.height <= 2


def test_limit_above_ten_thousand_returns_a_semantic_error(query_profile):
    plan = QueryPlan.model_validate({"limit": 20_000})
    with pytest.raises(SemanticValidationError, match="between 1 and 10000"):
        validate_query_plan(plan, query_profile)
