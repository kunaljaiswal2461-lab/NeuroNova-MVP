"""Safe execution boundary for deterministic direct-query plans.

The query LLM returns a ``QueryPlan`` JSON document (or ``UNSUPPORTED``),
never executable Polars. ``query_compiler`` is the sole producer of the
compiled expression. This module validates that compiler-owned expression as
a final defense, executes it in a restricted namespace, and renders its table.
"""
from __future__ import annotations

import ast
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import polars as pl
from pydantic import ValidationError

from app.core.config import POLARS_FUNCTION_SIGNATURES, Settings, get_settings
from app.core.logging import get_logger
from app.db.models.dataset import FileType
from agentic_engine.conversational.models import (
    QUERY_ROW_LIMIT,
    QUERY_TIMEOUT_SECONDS,
    QueryColumn,
    QueryResult,
)
from agentic_engine.conversational.prompts import query_plan as query_plan_prompt
from agentic_engine.conversational.query_allowlist import (
    ALLOWED_METHODS as _ALLOWED_METHODS,
    ALLOWED_NAMES as _ALLOWED_NAMES,
    ALLOWED_NODE_TYPES as _ALLOWED_NODE_TYPES,
    ALLOWED_PL_ATTRIBUTES as _ALLOWED_PL_ATTRIBUTES,
    ALLOWED_PL_FUNCTIONS as _ALLOWED_PL_FUNCTIONS,
    FORBIDDEN_KEYWORD_VALUES as _FORBIDDEN_KEYWORD_VALUES,
)
from agentic_engine.conversational.query_compiler import (
    SemanticValidationError,
    compile_query_plan,
)
from agentic_engine.conversational.query_plan import parse_query_plan_output
from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable
from agentic_engine.llm_engine.models import TokenUsage
from agentic_engine.profiler.loader import UnreadableDatasetError, load_dataset
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("conversational.query")

_COMPILER_ONLY_METHODS = frozenset({"lazy", "collect"})
_UNSUPPORTED_REPLY = (
    "I can help with dataset questions using filters, group by, count, average, "
    "sum, min, max, median, percentage, sorting, top-N results, and showing "
    "specific columns. Please rephrase your question using one of these operations."
)


class QueryValidationError(Exception):
    """Raised when compiler-created source falls outside the safe AST."""


@dataclass(frozen=True)
class QueryPlanExecution:
    """Outcome of planning, validation, compilation, and execution."""

    query_result: QueryResult | None
    reply: str | None
    unsupported: bool
    degraded: bool
    degraded_reason: str | None
    token_usage: TokenUsage


def validate_expression(
    expression: str,
    *,
    compiler_generated: bool = False,
) -> ast.Expression:
    """Apply the final AST and signature safety check to an expression.

    ``lazy`` and ``collect`` are accepted only for source created by the
    deterministic compiler. They remain disallowed for arbitrary source.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise QueryValidationError(f"syntax error: {exc.msg}") from exc

    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODE_TYPES:
            raise QueryValidationError(f"disallowed AST node: {type(node).__name__}")

        if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES:
            raise QueryValidationError(
                f"disallowed identifier: {node.id!r} (only df and pl are in scope)"
            )

        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise QueryValidationError(
                    f"disallowed attribute (underscore-prefixed): {node.attr!r}"
                )
            if isinstance(node.value, ast.Name) and node.value.id == "pl":
                if node.attr not in _ALLOWED_PL_ATTRIBUTES:
                    raise QueryValidationError(f"unknown pl.* function: pl.{node.attr}")
                continue
            if node.attr in _COMPILER_ONLY_METHODS and not compiler_generated:
                raise QueryValidationError(
                    f"compiler-only method is not allowed: {node.attr!r}"
                )
            if node.attr not in _ALLOWED_METHODS:
                raise QueryValidationError(f"disallowed method/attribute: {node.attr!r}")

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name) and func.value.id == "pl":
                    if func.attr not in _ALLOWED_PL_FUNCTIONS:
                        raise QueryValidationError(
                            f"unknown pl.* function: pl.{func.attr}"
                        )
                    signature = POLARS_FUNCTION_SIGNATURES.get(func.attr)
                    if signature is None:
                        raise QueryValidationError(
                            f"undocumented pl.* function: pl.{func.attr}"
                        )
                    _validate_argument_count(func.attr, node, signature)
            elif isinstance(func, ast.Name) and func.id in _ALLOWED_NAMES:
                raise QueryValidationError(f"bare function call not allowed: {func.id}(...)")

            for argument in node.args:
                if isinstance(argument, ast.Starred):
                    raise QueryValidationError("starred args not allowed")

            if isinstance(func, ast.Attribute):
                forbidden = _FORBIDDEN_KEYWORD_VALUES.get(func.attr, {})
                for keyword in node.keywords:
                    if (
                        keyword.arg in forbidden
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == forbidden[keyword.arg]
                    ):
                        raise QueryValidationError(
                            f"disallowed argument: {func.attr}({keyword.arg}={keyword.value.value!r})"
                        )

        if isinstance(node, ast.Subscript):
            slice_value = node.slice
            if isinstance(slice_value, ast.Index):  # pragma: no cover - py<3.9
                slice_value = slice_value.value
            if isinstance(slice_value, ast.Slice):
                continue
            if isinstance(slice_value, ast.Tuple):
                if all(isinstance(item, (ast.Constant, ast.Slice)) for item in slice_value.elts):
                    continue
                raise QueryValidationError("subscript keys must be literal constants")
            if not isinstance(slice_value, ast.Constant):
                raise QueryValidationError("subscript keys must be literal constants")

    return tree


def _validate_argument_count(
    function: str,
    node: ast.Call,
    signature: tuple[int, int | None],
) -> None:
    minimum, maximum = signature
    actual = len(node.args) + len(node.keywords)
    if actual < minimum or (maximum is not None and actual > maximum):
        expected = (
            str(minimum)
            if minimum == maximum
            else f"{minimum}+"
            if maximum is None
            else f"{minimum} to {maximum}"
        )
        raise QueryValidationError(
            f"pl.{function} expected {expected} argument(s), got {actual}"
        )


async def execute_query_plan(
    message: str,
    *,
    raw_path: Any,
    file_type: FileType,
    profile: ProfileReport,
    schema_snapshot: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
    llm: LLMClient | None,
    settings: Settings | None = None,
) -> QueryPlanExecution:
    """Run the permanent plan-only QUERY path with no free-form fallback."""
    settings = settings or get_settings()
    if llm is None:
        return _failed_execution(
            "The query service is unavailable because no LLM is configured.",
            reason="LLM client is not configured",
        )

    try:
        raw_output, token_usage = await _request_plan(
            llm=llm,
            message=message,
            schema_snapshot=schema_snapshot,
            history=history,
            settings=settings,
        )
    except LLMUnavailable as exc:
        return _failed_execution(
            "The query service is temporarily unavailable. Please try again.",
            reason=f"LLM unavailable: {exc}",
        )

    try:
        plan = parse_query_plan_output(raw_output)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        logger.warning("invalid_query_plan", raw_output=raw_output, parse_error=str(exc))
        return _unsupported_execution(token_usage)

    if plan is None:
        logger.info("unsupported_query", user_message=message)
        return _unsupported_execution(token_usage)

    try:
        compiled_query = compile_query_plan(plan, profile)
    except SemanticValidationError as exc:
        logger.info("semantic_validation_failed", **exc.as_dict())
        return QueryPlanExecution(
            query_result=None,
            reply=exc.user_message(),
            unsupported=False,
            degraded=False,
            degraded_reason=None,
            token_usage=token_usage,
        )
    except Exception as exc:  # pragma: no cover - compiler invariant guard
        logger.error(
            "compiler_error",
            query_plan=plan.model_dump(mode="json"),
            error=str(exc),
        )
        return _failed_execution(
            "I could not compile that query. Please try rephrasing it.",
            reason=f"compiler error: {exc}",
            token_usage=token_usage,
        )

    try:
        checked_tree = validate_expression(
            compiled_query.expression,
            compiler_generated=True,
        )
        if ast.dump(checked_tree) != ast.dump(compiled_query.tree):
            raise QueryValidationError("compiled source does not match compiler AST")
    except QueryValidationError as exc:  # pragma: no cover - compiler invariant guard
        logger.error(
            "compiler_error",
            query_plan=plan.model_dump(mode="json"),
            error=str(exc),
        )
        return _failed_execution(
            "I could not validate that compiled query.",
            reason=f"compiler safety check failed: {exc}",
            token_usage=token_usage,
        )

    try:
        frame = await asyncio.to_thread(load_dataset, raw_path, file_type)
    except UnreadableDatasetError as exc:
        return _result_error(
            compiled_query.expression,
            f"could not load dataset: {exc}",
            token_usage=token_usage,
        )

    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(compiled_query.execute, frame),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return _result_error(
            compiled_query.expression,
            f"query exceeded {QUERY_TIMEOUT_SECONDS}s timeout",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            token_usage=token_usage,
        )
    except Exception as exc:
        logger.error(
            "compiler_error",
            query_plan=plan.model_dump(mode="json"),
            error=str(exc),
        )
        return _result_error(
            compiled_query.expression,
            f"execution failed: {exc}",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            token_usage=token_usage,
        )

    return QueryPlanExecution(
        query_result=_render_result(
            compiled_query.expression,
            result,
            (time.perf_counter() - started) * 1000.0,
        ),
        reply=None,
        unsupported=False,
        degraded=False,
        degraded_reason=None,
        token_usage=token_usage,
    )


async def _request_plan(
    *,
    llm: LLMClient,
    message: str,
    schema_snapshot: list[dict[str, Any]],
    history: list[dict[str, str]] | None,
    settings: Settings,
) -> tuple[str, TokenUsage]:
    """Use text streaming so ``UNSUPPORTED`` remains a literal token."""
    pieces: list[str] = []
    usage = TokenUsage()
    user_prompt = query_plan_prompt.build_user(
        message,
        schema_snapshot,
        history=history,
    )
    async for delta in llm.chat_stream(
        model=settings.openai_chat_model,
        system=query_plan_prompt.SYSTEM,
        user=user_prompt,
        history=None,
        max_tokens=600,
        temperature=0.0,
    ):
        if delta.finished:
            usage.add(delta.input_tokens, delta.output_tokens)
        elif delta.text:
            pieces.append(delta.text)
    return "".join(pieces), usage


def _unsupported_execution(token_usage: TokenUsage) -> QueryPlanExecution:
    return QueryPlanExecution(
        query_result=None,
        reply=_UNSUPPORTED_REPLY,
        unsupported=True,
        degraded=False,
        degraded_reason=None,
        token_usage=token_usage,
    )


def _failed_execution(
    reply: str,
    *,
    reason: str,
    token_usage: TokenUsage | None = None,
) -> QueryPlanExecution:
    return QueryPlanExecution(
        query_result=None,
        reply=reply,
        unsupported=False,
        degraded=True,
        degraded_reason=reason,
        token_usage=token_usage or TokenUsage(),
    )


def _result_error(
    expression: str,
    error: str,
    *,
    elapsed_ms: float = 0.0,
    token_usage: TokenUsage,
) -> QueryPlanExecution:
    return QueryPlanExecution(
        query_result=QueryResult(expression=expression, error=error, elapsed_ms=elapsed_ms),
        reply=None,
        unsupported=False,
        degraded=True,
        degraded_reason=error,
        token_usage=token_usage,
    )


def _render_result(expression: str, frame: pl.DataFrame, elapsed_ms: float) -> QueryResult:
    total_rows = frame.height
    truncated = total_rows > QUERY_ROW_LIMIT
    visible = frame.head(QUERY_ROW_LIMIT) if truncated else frame
    return QueryResult(
        expression=expression,
        columns=[
            QueryColumn(name=name, dtype=str(dtype))
            for name, dtype in zip(visible.columns, visible.dtypes)
        ],
        rows=[list(row) for row in visible.rows()],
        row_count=total_rows,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )


def schema_snapshot_from_profile(profile_columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the compact, non-executable schema supplied to the plan LLM."""
    snapshot: list[dict[str, Any]] = []
    for column in profile_columns:
        if column.get("schema_truncated"):
            snapshot.append({
                "schema_truncated": True,
                "included_column_count": column["included_column_count"],
                "total_column_count": column["total_column_count"],
            })
            continue
        entry: dict[str, Any] = {
            "name": column["name"],
            "dtype": column.get("dtype") or column.get("polars_dtype") or "Unknown",
        }
        if column.get("semantic_type"):
            entry["semantic_type"] = column["semantic_type"]
        snapshot.append(entry)
    return snapshot


__all__ = [
    "QueryPlanExecution",
    "QueryValidationError",
    "execute_query_plan",
    "schema_snapshot_from_profile",
    "validate_expression",
]
