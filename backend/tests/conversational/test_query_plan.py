"""Unit tests for the LLM-facing, non-executable QueryPlan contract."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentic_engine.conversational.prompts import query_plan as query_plan_prompt
from agentic_engine.conversational.query_plan import (
    QueryPlan,
    parse_query_plan_output,
)


def test_simple_filter_output_produces_a_plan_without_metrics():
    plan = parse_query_plan_output(json.dumps({
        "filters": [{"column": "country", "operator": "equals", "value": "US"}],
    }))

    assert isinstance(plan, QueryPlan)
    assert plan.filters[0].column == "country"
    assert plan.filters[0].operator == "equals"
    assert plan.metrics == []


def test_group_by_mean_output_produces_the_expected_plan():
    plan = parse_query_plan_output(json.dumps({
        "group_by": {"columns": ["country"]},
        "metrics": [{"type": "mean", "column": "revenue", "name": "avg_revenue"}],
    }))

    assert isinstance(plan, QueryPlan)
    assert plan.group_by is not None
    assert plan.group_by.columns == ["country"]
    assert plan.metrics[0].type == "mean"
    assert plan.metrics[0].column == "revenue"


def test_unsupported_output_returns_no_plan():
    assert parse_query_plan_output("UNSUPPORTED") is None


def test_query_plan_rejects_unknown_llm_fields():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        parse_query_plan_output('{"filters": [], "arbitrary_code": "pl.read_csv()"}')


def test_prompt_requires_json_or_the_exact_unsupported_token():
    assert "Return ONLY" in query_plan_prompt.SYSTEM
    assert "UNSUPPORTED" in query_plan_prompt.SYSTEM
    assert "Never return Polars" in query_plan_prompt.SYSTEM
    for operator in (
        "equals", "not_equals", "greater_than", "less_than",
        "greater_or_equal", "less_or_equal", "contains", "not_contains",
        "is_null", "is_not_null",
    ):
        assert operator in query_plan_prompt.SYSTEM
    for metric in (
        "count", "count_distinct", "mean", "sum", "min", "max",
        "median", "percentage_of_all_rows", "percentage_of_group",
    ):
        assert metric in query_plan_prompt.SYSTEM
