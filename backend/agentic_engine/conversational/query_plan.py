"""Structured, non-executable plans for direct dataset queries.

The LLM is allowed to choose only this JSON shape.  It never supplies a
Polars expression; the deterministic compiler turns a validated
``QueryPlan`` into the single expression that is later executed.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


FilterOperator = Literal[
    "equals",
    "not_equals",
    "greater_than",
    "less_than",
    "greater_or_equal",
    "less_or_equal",
    "contains",
    "not_contains",
    "is_null",
    "is_not_null",
]
MetricType = Literal[
    "count",
    "count_distinct",
    "mean",
    "sum",
    "min",
    "max",
    "median",
    "percentage_of_all_rows",
    "percentage_of_group",
]
SortDirection = Literal["asc", "desc"]
UNSUPPORTED_QUERY = "UNSUPPORTED"


class FilterCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1)
    operator: FilterOperator
    value: str | int | float | bool | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "FilterCondition":
        null_operators = {"is_null", "is_not_null"}
        if self.operator in null_operators and self.value is not None:
            raise ValueError(f"{self.operator} does not accept a value")
        if self.operator not in null_operators and self.value is None:
            raise ValueError(f"{self.operator} requires a value")
        return self


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: MetricType
    column: str | None = None
    name: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_column_requirement(self) -> "MetricDefinition":
        no_column_metrics = {
            "count",
            "percentage_of_all_rows",
            "percentage_of_group",
        }
        if self.type in no_column_metrics and self.column is not None:
            raise ValueError(f"{self.type} must not specify a column")
        if self.type not in no_column_metrics and self.column is None:
            raise ValueError(f"{self.type} requires a column")
        return self


class GroupByDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(min_length=1)


class SortDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1)
    direction: SortDirection


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: list[FilterCondition] = Field(default_factory=list)
    group_by: GroupByDefinition | None = None
    metrics: list[MetricDefinition] = Field(default_factory=list)
    sort: SortDefinition | None = None
    limit: int | None = None
    select_columns: list[str] = Field(default_factory=list)


def parse_query_plan_output(raw_output: str) -> QueryPlan | None:
    """Parse the exact text returned by the plan LLM.

    ``None`` represents the deliberate ``UNSUPPORTED`` capability boundary.
    Invalid JSON and invalid plans intentionally propagate their descriptive
    exceptions to the QUERY branch, where they are logged and returned as the
    same user-facing unsupported response.
    """
    normalized = raw_output.strip()
    if normalized == UNSUPPORTED_QUERY:
        return None
    payload = json.loads(normalized)
    return QueryPlan.model_validate(payload)


__all__ = [
    "FilterCondition",
    "GroupByDefinition",
    "MetricDefinition",
    "QueryPlan",
    "SortDefinition",
    "UNSUPPORTED_QUERY",
    "parse_query_plan_output",
]
