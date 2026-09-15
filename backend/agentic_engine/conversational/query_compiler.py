"""Schema-aware validation and deterministic QueryPlan → Polars compilation.

This is the only conversational component that creates executable Polars
expressions.  It builds a Python AST from validated, finite QueryPlan choices;
the LLM never contributes source code.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

import polars as pl

from agentic_engine.conversational.query_plan import (
    FilterCondition,
    MetricDefinition,
    QueryPlan,
)
from agentic_engine.profiler.report import ProfileReport


_NUMERIC_DTYPE_MARKERS = (
    "int", "uint", "float", "decimal", "number", "numeric",
)
_STRING_DTYPE_MARKERS = (
    "str", "string", "utf8", "categorical", "enum", "category", "object",
)
_NUMERIC_OPERATORS = {
    "greater_than", "less_than", "greater_or_equal", "less_or_equal",
}
_STRING_OPERATORS = {"contains", "not_contains"}
_NUMERIC_METRICS = {"mean", "sum", "min", "max", "median"}
_TEMP_GROUP_COUNT = "__neuronova_group_count"


@dataclass(frozen=True)
class SemanticValidationError(ValueError):
    """A structured, user-safe reason that a plan cannot run."""

    field: str
    column: str | None
    actual_dtype: str | None
    reason: str

    def __str__(self) -> str:
        details: list[str] = [self.reason]
        if self.column is not None:
            details.append(f"column '{self.column}'")
        if self.actual_dtype is not None:
            details.append(f"dtype {self.actual_dtype}")
        return f"{self.field}: " + "; ".join(details)

    def as_dict(self) -> dict[str, str | None]:
        return {
            "field": self.field,
            "column": self.column,
            "actual_dtype": self.actual_dtype,
            "reason": self.reason,
        }

    def user_message(self) -> str:
        return f"I could not run that query: {self.reason}"


@dataclass(frozen=True)
class _ColumnInfo:
    dtype: str
    inferred_kind: str | None

    @property
    def _markers(self) -> str:
        return f"{self.dtype} {self.inferred_kind or ''}".lower()

    @property
    def is_numeric(self) -> bool:
        return any(marker in self._markers for marker in _NUMERIC_DTYPE_MARKERS)

    @property
    def is_string_like(self) -> bool:
        return any(marker in self._markers for marker in _STRING_DTYPE_MARKERS)


def validate_query_plan(plan: QueryPlan, profile: ProfileReport) -> None:
    """Validate QueryPlan semantics against the persisted dataset profile.

    The checks deliberately run before the dataset is loaded.  Invalid column
    names and incompatible operations therefore become clear capability
    messages instead of opaque Polars runtime exceptions.
    """
    columns = _column_catalog(profile)

    for index, condition in enumerate(plan.filters):
        info = _require_column(columns, condition.column, f"filters[{index}].column")
        if condition.operator in _NUMERIC_OPERATORS:
            _require_numeric(info, f"filters[{index}]", condition.column)
            if isinstance(condition.value, bool) or not isinstance(
                condition.value, (int, float)
            ):
                raise SemanticValidationError(
                    field=f"filters[{index}].value",
                    column=condition.column,
                    actual_dtype=info.dtype,
                    reason="numeric comparisons require a numeric value",
                )
        elif condition.operator in _STRING_OPERATORS:
            _require_string_like(info, f"filters[{index}]", condition.column)
            if not isinstance(condition.value, str):
                raise SemanticValidationError(
                    field=f"filters[{index}].value",
                    column=condition.column,
                    actual_dtype=info.dtype,
                    reason=f"{condition.operator} requires a string value",
                )

    for index, metric in enumerate(plan.metrics):
        field = f"metrics[{index}].column"
        if metric.column is not None:
            info = _require_column(columns, metric.column, field)
            if metric.type in _NUMERIC_METRICS:
                _require_numeric(info, f"metrics[{index}]", metric.column)
        if metric.type in {"percentage_of_all_rows", "percentage_of_group"}:
            if metric.column is not None:
                raise SemanticValidationError(
                    field=field,
                    column=metric.column,
                    actual_dtype=None,
                    reason=f"{metric.type} does not accept a column",
                )
            if metric.type == "percentage_of_group" and plan.group_by is None:
                raise SemanticValidationError(
                    field=f"metrics[{index}].type",
                    column=None,
                    actual_dtype=None,
                    reason="percentage_of_group requires group_by",
                )

    if len({metric.name for metric in plan.metrics}) != len(plan.metrics):
        raise SemanticValidationError(
            field="metrics",
            column=None,
            actual_dtype=None,
            reason="metric output names must be unique",
        )
    if any(metric.name == _TEMP_GROUP_COUNT for metric in plan.metrics):
        raise SemanticValidationError(
            field="metrics.name",
            column=_TEMP_GROUP_COUNT,
            actual_dtype=None,
            reason="this output name is reserved internally",
        )

    if plan.group_by is not None:
        for index, column in enumerate(plan.group_by.columns):
            _require_column(columns, column, f"group_by.columns[{index}]")
        if not plan.metrics:
            raise SemanticValidationError(
                field="group_by",
                column=None,
                actual_dtype=None,
                reason="group_by requires at least one metric",
            )

    for index, column in enumerate(plan.select_columns):
        _require_column(columns, column, f"select_columns[{index}]")
    if plan.select_columns and (plan.group_by is not None or plan.metrics):
        raise SemanticValidationError(
            field="select_columns",
            column=None,
            actual_dtype=None,
            reason="select_columns can only be used without grouping or metrics",
        )

    if plan.sort is not None:
        sortable = set(columns) | {metric.name for metric in plan.metrics}
        if plan.sort.column not in sortable:
            raise SemanticValidationError(
                field="sort.column",
                column=plan.sort.column,
                actual_dtype=None,
                reason="sort column does not exist in the dataset or metric outputs",
            )

    if plan.limit is not None and not 1 <= plan.limit <= 10_000:
        raise SemanticValidationError(
            field="limit",
            column=None,
            actual_dtype=None,
            reason="limit must be between 1 and 10000",
        )


def _column_catalog(profile: ProfileReport) -> dict[str, _ColumnInfo]:
    inferred_kind_by_name = {
        column.name: column.inferred_kind for column in profile.stats.columns
    }
    return {
        column.name: _ColumnInfo(
            dtype=column.polars_dtype or column.dtype,
            inferred_kind=inferred_kind_by_name.get(column.name),
        )
        for column in profile.schema_.columns
    }


def _require_column(
    columns: dict[str, _ColumnInfo], column: str, field: str
) -> _ColumnInfo:
    info = columns.get(column)
    if info is None:
        raise SemanticValidationError(
            field=field,
            column=column,
            actual_dtype=None,
            reason="column does not exist in this dataset",
        )
    return info


def _require_numeric(info: _ColumnInfo, field: str, column: str) -> None:
    if not info.is_numeric:
        raise SemanticValidationError(
            field=field,
            column=column,
            actual_dtype=info.dtype,
            reason="this operation requires a numeric column",
        )


def _require_string_like(info: _ColumnInfo, field: str, column: str) -> None:
    if not info.is_string_like:
        raise SemanticValidationError(
            field=field,
            column=column,
            actual_dtype=info.dtype,
            reason="this operation requires a string or categorical column",
        )


@dataclass(frozen=True)
class CompiledQuery:
    """A backend-created AST and the exact executable source it represents."""

    expression: str
    tree: ast.Expression

    def execute(self, df: pl.DataFrame) -> pl.DataFrame:
        """Execute only the compiler-owned expression against ``df``."""
        compiled = compile(self.tree, filename="<query-plan>", mode="eval")
        result = eval(  # noqa: S307 - AST is created solely by QueryPlanCompiler
            compiled,
            {"__builtins__": {}, "df": df, "pl": pl},
            {},
        )
        if not isinstance(result, pl.DataFrame):  # defensive compiler invariant
            raise TypeError(f"compiled query returned {type(result).__name__}, not DataFrame")
        return result


class QueryPlanCompiler:
    """Turn a validated plan into a fixed-order lazy Polars expression."""

    def __init__(self, plan: QueryPlan) -> None:
        self.plan = plan

    def compile(self) -> CompiledQuery:
        frame: ast.expr = _method(_name("df"), "lazy")
        filter_expression = self._compile_filters(self.plan.filters)
        if filter_expression is not None:
            frame = _method(frame, "filter", filter_expression)

        percentage_group_names = [
            metric.name
            for metric in self.plan.metrics
            if metric.type == "percentage_of_group"
        ]
        if self.plan.group_by is not None:
            frame = self._compile_groupby(frame, percentage_group_names)
        elif self.plan.metrics:
            frame = _method(frame, "select", _list(self._compile_metrics()))
        elif self.plan.select_columns:
            frame = self._compile_select(frame)

        if self.plan.sort is not None:
            frame = self._compile_sort(frame)
        if self.plan.limit is not None:
            frame = self._compile_limit(frame)

        frame = _method(frame, "collect")
        tree = ast.Expression(body=frame)
        ast.fix_missing_locations(tree)
        return CompiledQuery(expression=ast.unparse(tree), tree=tree)

    def _compile_filters(
        self, filters: list[FilterCondition]
    ) -> ast.expr | None:
        expressions = [self._compile_filter(condition) for condition in filters]
        if not expressions:
            return None
        combined = expressions[0]
        for expression in expressions[1:]:
            combined = ast.BinOp(left=combined, op=ast.BitAnd(), right=expression)
        return combined

    def _compile_filter(self, condition: FilterCondition) -> ast.expr:
        column = _pl_col(condition.column)
        value = _constant(condition.value)
        comparisons: dict[str, ast.cmpop] = {
            "equals": ast.Eq(),
            "not_equals": ast.NotEq(),
            "greater_than": ast.Gt(),
            "less_than": ast.Lt(),
            "greater_or_equal": ast.GtE(),
            "less_or_equal": ast.LtE(),
        }
        if condition.operator in comparisons:
            return ast.Compare(left=column, ops=[comparisons[condition.operator]], comparators=[value])
        if condition.operator == "contains":
            return _method(_attr(column, "str"), "contains", value)
        if condition.operator == "not_contains":
            return ast.UnaryOp(
                op=ast.Invert(),
                operand=_method(_attr(column, "str"), "contains", value),
            )
        if condition.operator == "is_null":
            return _method(column, "is_null")
        if condition.operator == "is_not_null":
            return _method(column, "is_not_null")
        raise ValueError(f"unsupported filter operator: {condition.operator}")

    def _compile_groupby(
        self, frame: ast.expr, percentage_group_names: list[str]
    ) -> ast.expr:
        assert self.plan.group_by is not None
        group_columns = _list([_constant(column) for column in self.plan.group_by.columns])
        metrics = self._compile_metrics()
        if percentage_group_names:
            metrics.append(_method(_pl("len"), "alias", _constant(_TEMP_GROUP_COUNT)))
        frame = _method(_method(frame, "group_by", group_columns), "agg", _list(metrics))

        if percentage_group_names:
            percentage_expressions = []
            for name in percentage_group_names:
                ratio = ast.BinOp(
                    left=_pl_col(_TEMP_GROUP_COUNT),
                    op=ast.Div(),
                    right=_method(_pl_col(_TEMP_GROUP_COUNT), "sum"),
                )
                percentage_expressions.append(
                    _method(
                        ast.BinOp(left=ratio, op=ast.Mult(), right=_constant(100)),
                        "alias",
                        _constant(name),
                    )
                )
            frame = _method(frame, "with_columns", _list(percentage_expressions))
            frame = _method(frame, "drop", _constant(_TEMP_GROUP_COUNT))
        return frame

    def _compile_metrics(self) -> list[ast.expr]:
        return [self._compile_metric(metric) for metric in self.plan.metrics if metric.type != "percentage_of_group"]

    def _compile_metric(self, metric: MetricDefinition) -> ast.expr:
        if metric.type == "count":
            expression = _pl("len")
        elif metric.type == "percentage_of_all_rows":
            # ``df.height`` is evaluated from the original eager frame, before
            # filtering.  The guard makes an empty upload return 0.0 instead
            # of dividing by zero.
            height = _attr(_name("df"), "height")
            condition = ast.Compare(left=height, ops=[ast.Gt()], comparators=[_constant(0)])
            ratio = ast.BinOp(
                left=ast.BinOp(left=_pl("len"), op=ast.Div(), right=height),
                op=ast.Mult(),
                right=_constant(100),
            )
            expression = _method(
                _method(
                    _method(_pl("when", condition), "then", ratio),
                    "otherwise",
                    _pl("lit", _constant(0.0)),
                ),
                "alias",
                _constant(metric.name),
            )
            return expression
        else:
            assert metric.column is not None
            method = {
                "count_distinct": "n_unique",
                "mean": "mean",
                "sum": "sum",
                "min": "min",
                "max": "max",
                "median": "median",
            }.get(metric.type)
            if method is None:
                raise ValueError(f"unsupported metric type: {metric.type}")
            expression = _method(_pl_col(metric.column), method)
        return _method(expression, "alias", _constant(metric.name))

    def _compile_sort(self, frame: ast.expr) -> ast.expr:
        assert self.plan.sort is not None
        return _method(
            frame,
            "sort",
            _constant(self.plan.sort.column),
            keywords=[ast.keyword(arg="descending", value=_constant(self.plan.sort.direction == "desc"))],
        )

    def _compile_limit(self, frame: ast.expr) -> ast.expr:
        assert self.plan.limit is not None
        return _method(frame, "limit", _constant(self.plan.limit))

    def _compile_select(self, frame: ast.expr) -> ast.expr:
        return _method(frame, "select", _list([_constant(column) for column in self.plan.select_columns]))


def compile_query_plan(plan: QueryPlan, profile: ProfileReport) -> CompiledQuery:
    """Validate then compile a plan, preserving one public entry point."""
    validate_query_plan(plan, profile)
    return QueryPlanCompiler(plan).compile()


def _name(identifier: str) -> ast.Name:
    return ast.Name(id=identifier, ctx=ast.Load())


def _attr(value: ast.expr, attribute: str) -> ast.Attribute:
    return ast.Attribute(value=value, attr=attribute, ctx=ast.Load())


def _method(
    value: ast.expr,
    method: str,
    *args: ast.expr,
    keywords: list[ast.keyword] | None = None,
) -> ast.Call:
    return ast.Call(
        func=_attr(value, method),
        args=list(args),
        keywords=keywords or [],
    )


def _pl(function: str, *args: ast.expr) -> ast.Call:
    return ast.Call(func=_attr(_name("pl"), function), args=list(args), keywords=[])


def _pl_col(column: str) -> ast.Call:
    return _pl("col", _constant(column))


def _constant(value: Any) -> ast.Constant:
    return ast.Constant(value=value)


def _list(elements: list[ast.expr]) -> ast.List:
    return ast.List(elts=elements, ctx=ast.Load())


__all__ = [
    "CompiledQuery",
    "QueryPlanCompiler",
    "SemanticValidationError",
    "compile_query_plan",
    "validate_query_plan",
]
