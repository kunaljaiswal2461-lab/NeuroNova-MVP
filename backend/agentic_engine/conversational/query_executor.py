"""NL → Polars expression executor with AST sandboxing.

Pipeline for one query turn:

  1. **Codegen** — single JSON-mode LLM call (gpt-4o) produces a Polars
     expression plus a one-sentence explanation. Prompted to stay inside
     a tight grammar (see ``prompts/query_codegen.py``).
  2. **AST validation** — :func:`validate_expression` walks the parsed
     AST and rejects anything outside an explicit whitelist of node
     types and attribute names. Default-deny: an unknown construct fails
     fast rather than reaching ``eval``.
  3. **Sandboxed eval** — only ``df`` and ``pl`` are exposed; built-ins
     are stripped via ``{"__builtins__": {}}``. The call is wrapped in a
     thread with a wall-clock timeout so a runaway computation cannot
     stall the event loop.
  4. **Render** — the resulting DataFrame / Series / scalar is coerced
     to a :class:`QueryResult` with at most :data:`QUERY_ROW_LIMIT`
     rows; anything beyond that flips the ``truncated`` flag.

Failure modes (LLM unavailable, AST rejection, runtime exception,
timeout) are absorbed by :func:`execute_query`, which always returns a
structurally valid :class:`QueryResult` with ``error`` set. The chat
agent surfaces those as degraded turns rather than 500s.

Why Polars and not Pandas: Polars expressions are first-class objects
with a narrow, immutable API (filter / select / agg / sort / etc.) which
maps cleanly onto a small AST whitelist. Pandas' chained-mutation style
(``df["x"] = ...``, ``inplace=True``) would be harder to constrain.
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import decimal as _decimal
import time
import uuid
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.dataset import FileType
from agentic_engine.conversational.models import (
    QUERY_ROW_LIMIT,
    QUERY_TIMEOUT_SECONDS,
    QueryColumn,
    QueryResult,
)
from agentic_engine.conversational.prompts import query_codegen
from agentic_engine.conversational.query_allowlist import (
    ALLOWED_METHODS as _ALLOWED_METHODS,
    ALLOWED_NAMES as _ALLOWED_NAMES,
    ALLOWED_NODE_TYPES as _ALLOWED_NODE_TYPES,
    ALLOWED_PL_ATTRIBUTES as _ALLOWED_PL_ATTRIBUTES,
    ALLOWED_PL_FUNCTIONS as _ALLOWED_PL_FUNCTIONS,
    FORBIDDEN_KEYWORD_VALUES as _FORBIDDEN_KEYWORD_VALUES,
)
from agentic_engine.conversational.query_compat import normalise_expression
from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable
from agentic_engine.profiler.loader import UnreadableDatasetError, load_dataset


logger = get_logger("conversational.query")


# ── AST whitelist ────────────────────────────────────────────────────────────
#
# The whitelist itself lives in :mod:`query_allowlist` so the codegen
# prompt and this validator share one definition. Default-deny is the key
# property — we never trust the model to stay on the path; the AST is the
# enforcement boundary.

# One LLM repair round is allowed when the first expression is rejected
# by the validator or fails at runtime. The model is shown the exact
# failure and the grammar, which resolves the vast majority of
# API-drift mistakes without a second user round-trip.
MAX_CODEGEN_ATTEMPTS = 2


class QueryValidationError(Exception):
    """Raised internally by :func:`validate_expression` when the AST
    contains a disallowed construct. The executor catches this and
    surfaces it as a :class:`QueryResult` with ``error`` set; it is not
    propagated to the chat agent.
    """


# ── AST validation ───────────────────────────────────────────────────────────

def validate_expression(expression: str) -> ast.Expression:
    """Parse and walk the AST, rejecting any node outside the whitelist.

    Returns the parsed ``ast.Expression`` on success so the caller can
    re-use it for compilation without re-parsing. Raises
    :class:`QueryValidationError` on rejection.
    """
    # ``mode="eval"`` rejects statements (assignments, imports, etc.) at
    # the parse step, so most attack surfaces are gone before we even
    # walk the tree.
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise QueryValidationError(f"syntax error: {exc.msg}") from exc

    for node in ast.walk(tree):
        node_type = type(node)
        if node_type not in _ALLOWED_NODE_TYPES:
            raise QueryValidationError(
                f"disallowed AST node: {node_type.__name__}"
            )

        if isinstance(node, ast.Name):
            if node.id not in _ALLOWED_NAMES:
                raise QueryValidationError(
                    f"disallowed identifier: {node.id!r} "
                    f"(only df and pl are in scope)"
                )

        if isinstance(node, ast.Attribute):
            # Dunder attribute access is the canonical sandbox-escape
            # vector; reject anything starting with an underscore.
            if node.attr.startswith("_"):
                raise QueryValidationError(
                    f"disallowed attribute (underscore-prefixed): {node.attr!r}"
                )
            # ``pl.<x>`` attribute access is permitted when ``x`` is in
            # the module-level whitelist (functions like ``col`` / dtypes
            # like ``Float64``). The corresponding Call node is further
            # validated below when ``pl.<fn>(...)`` is invoked.
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "pl"
                and node.attr in _ALLOWED_PL_ATTRIBUTES
            ):
                continue
            # Anything else attached to a disallowed identifier (e.g.
            # ``os.system``) gets a clearer error at the Name walk
            # below; here we focus on attribute-name rejection.
            if (
                isinstance(node.value, ast.Name)
                and node.value.id not in _ALLOWED_NAMES
            ):
                raise QueryValidationError(
                    f"disallowed identifier: {node.value.id!r} "
                    f"(only df and pl are in scope)"
                )
            if node.attr not in _ALLOWED_METHODS:
                raise QueryValidationError(
                    f"disallowed method/attribute: {node.attr!r}"
                )

        if isinstance(node, ast.Call):
            # Validate the function being called. The allowed shapes are:
            #   * ``pl.<allowed_fn>(...)``  — module-level Polars helper
            #   * ``<expr>.<allowed_method>(...)`` — chained method call
            #   * ``df(...)`` style bare calls are forbidden because they
            #     never make sense for our two whitelisted identifiers.
            func = node.func
            if isinstance(func, ast.Attribute):
                if (
                    isinstance(func.value, ast.Name)
                    and func.value.id == "pl"
                    and func.attr not in _ALLOWED_PL_FUNCTIONS
                ):
                    raise QueryValidationError(
                        f"disallowed pl.<x> function: pl.{func.attr}"
                    )
                # Other attribute calls (df.filter, expr.alias, etc.)
                # are filtered by the Attribute whitelist above.
            elif isinstance(func, ast.Name) and func.id in _ALLOWED_NAMES:
                # ``df(...)`` or ``pl(...)`` — both nonsensical for our
                # whitelisted identifiers; reject as a bare call.
                raise QueryValidationError(
                    f"bare function call not allowed: {func.id}(...)"
                )
            # Anything else (``__import__(...)``, ``Lambda(...)``, etc.)
            # is rejected by the dedicated walks: the Name check rejects
            # unknown identifiers with the clearer "disallowed identifier"
            # message; non-allowed node types (Lambda, Comprehension)
            # never enter the walk at all (they're not in
            # _ALLOWED_NODE_TYPES).

            # No starred args (``f(*xs)``) — bypasses kwargs intent.
            for arg in node.args:
                if isinstance(arg, ast.Starred):
                    raise QueryValidationError("starred args not allowed")

            # Keyword values that turn a read-only method into a memory
            # amplifier (``join(how="cross")``, ``sample(with_replacement=True)``).
            if isinstance(func, ast.Attribute):
                forbidden = _FORBIDDEN_KEYWORD_VALUES.get(func.attr, {})
                for kw in node.keywords:
                    if (
                        kw.arg in forbidden
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value == forbidden[kw.arg]
                    ):
                        raise QueryValidationError(
                            f"disallowed argument: {func.attr}({kw.arg}={kw.value.value!r})"
                        )

        if isinstance(node, ast.Subscript):
            # df["col"] is fine; df[some_expr] is rejected — we want
            # column names to be literal so the generated code is
            # auditable.
            slice_value = node.slice
            if isinstance(slice_value, ast.Index):  # py<3.9 compat
                slice_value = slice_value.value  # type: ignore[attr-defined]
            if isinstance(slice_value, ast.Slice):
                continue
            if isinstance(slice_value, ast.Tuple):
                # ``df[0, "col"]`` / ``df[0, 0]`` — every element must be a
                # literal or slice so the access stays auditable.
                if all(
                    isinstance(elt, (ast.Constant, ast.Slice))
                    for elt in slice_value.elts
                ):
                    continue
                raise QueryValidationError(
                    "subscript keys must be literal constants"
                )
            if not isinstance(slice_value, ast.Constant):
                raise QueryValidationError(
                    "subscript keys must be literal constants"
                )

    return tree


# ── execution ────────────────────────────────────────────────────────────────

class _QueryFailure(Exception):
    """Internal: a validation or runtime failure that is eligible for one
    LLM repair round. Carries the (normalised) expression that failed so
    the repair prompt can show the model exactly what went wrong."""

    def __init__(self, expression: str, error: str, *, elapsed_ms: float = 0.0):
        super().__init__(error)
        self.expression = expression
        self.error = error
        self.elapsed_ms = elapsed_ms


async def execute_query(
    message: str,
    *,
    raw_path: Any,
    file_type: FileType,
    schema_snapshot: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
    llm: LLMClient | None,
    settings: Settings | None = None,
) -> QueryResult:
    """Run one NL→Polars turn end to end.

    Always returns a structurally valid :class:`QueryResult`. Failure
    modes (no LLM, AST rejection, runtime error, timeout, file
    unreadable) are encoded in the ``error`` field so the chat agent can
    surface them as a degraded turn.

    Flow per attempt: codegen → legacy-API normalisation → AST
    validation → sandboxed eval → render. If validation or eval fails on
    the first attempt, the model is given the failing expression and the
    exact error once (:data:`MAX_CODEGEN_ATTEMPTS`) and the loop repeats.
    Timeouts and unreadable datasets are not retried — a second identical
    attempt cannot fix either.
    """
    settings = settings or get_settings()

    if llm is None:
        return QueryResult(
            expression="",
            error="LLM client is not configured; cannot generate query",
        )

    # The dataset is loaded lazily, once, and shared across attempts.
    frame: pl.DataFrame | None = None

    async def _frame() -> pl.DataFrame:
        nonlocal frame
        if frame is None:
            frame = await asyncio.to_thread(load_dataset, raw_path, file_type)
        return frame

    previous_failure: _QueryFailure | None = None
    last_result: QueryResult | None = None

    for attempt in range(1, MAX_CODEGEN_ATTEMPTS + 1):
        # ── 1. Codegen (or repair). ────────────────────────────────────
        if previous_failure is None:
            user_prompt = query_codegen.build_user(
                message, schema_snapshot, history=history,
            )
        else:
            user_prompt = query_codegen.build_repair_user(
                message,
                schema_snapshot,
                failed_expression=previous_failure.expression,
                error=previous_failure.error,
                history=history,
            )
        try:
            codegen = await llm.chat_json(
                model=settings.openai_chat_model,
                system=query_codegen.SYSTEM,
                user=user_prompt,
                max_tokens=600,
                temperature=0.1 if attempt == 1 else 0.0,
            )
        except LLMUnavailable as exc:
            logger.warning("query.codegen_failed", error=str(exc), attempt=attempt)
            if last_result is not None:
                # Repair call failed — report the original, more useful error.
                return last_result
            return QueryResult(
                expression="",
                error=f"codegen unavailable: {exc}",
                attempts=attempt,
            )

        raw_expression = (codegen.content_json.get("expression") or "").strip()
        if not raw_expression:
            last_result = QueryResult(
                expression="",
                error="codegen returned no expression",
                attempts=attempt,
            )
            previous_failure = _QueryFailure("", last_result.error or "")
            continue

        # ── 2. Legacy-API normalisation + AST validation. ─────────────
        normalised = normalise_expression(raw_expression)
        expression = normalised.expression
        if normalised.changed:
            logger.info(
                "query.normalised",
                original=raw_expression,
                expression=expression,
                rewrites=normalised.rewrites,
            )

        try:
            result = await _validate_and_run(
                expression,
                frame_loader=_frame,
                rewrites=normalised.rewrites,
                attempt=attempt,
            )
        except UnreadableDatasetError as exc:
            logger.warning("query.unreadable_dataset", error=str(exc))
            return QueryResult(
                expression=expression,
                error=f"could not load dataset: {exc}",
                attempts=attempt,
                rewrites=normalised.rewrites,
            )
        except _QueryTimeout as exc:
            return QueryResult(
                expression=expression,
                elapsed_ms=exc.elapsed_ms,
                error=exc.error,
                attempts=attempt,
                rewrites=normalised.rewrites,
            )
        except _QueryFailure as exc:
            last_result = QueryResult(
                expression=expression,
                elapsed_ms=exc.elapsed_ms,
                error=exc.error,
                attempts=attempt,
                rewrites=normalised.rewrites,
            )
            previous_failure = exc
            continue

        return result

    assert last_result is not None  # loop always sets it before falling through
    return last_result


class _QueryTimeout(Exception):
    def __init__(self, error: str, elapsed_ms: float):
        super().__init__(error)
        self.error = error
        self.elapsed_ms = elapsed_ms


async def _validate_and_run(
    expression: str,
    *,
    frame_loader: Any,
    rewrites: list[str],
    attempt: int,
) -> QueryResult:
    """Validate, compile, evaluate and render one candidate expression.

    Raises :class:`_QueryFailure` for repairable failures (AST rejection,
    compile error, runtime exception, unsupported result type),
    :class:`_QueryTimeout` for wall-clock overrun, and lets
    :class:`UnreadableDatasetError` propagate.
    """
    try:
        tree = validate_expression(expression)
    except QueryValidationError as exc:
        logger.warning(
            "query.rejected_by_ast",
            expression=expression,
            error=str(exc),
            attempt=attempt,
        )
        raise _QueryFailure(expression, f"rejected by AST validator: {exc}")

    df = await frame_loader()

    try:
        compiled = compile(tree, filename="<nl-query>", mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise _QueryFailure(expression, f"compile failed: {exc}")

    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_eval_sandboxed, compiled, df),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        elapsed = (time.perf_counter() - started) * 1000.0
        logger.warning("query.timeout", expression=expression, elapsed_ms=elapsed)
        raise _QueryTimeout(
            f"query exceeded {QUERY_TIMEOUT_SECONDS}s timeout", elapsed,
        )
    except Exception as exc:
        # Polars wraps a wide variety of column-not-found / dtype-mismatch
        # / out-of-range errors; surface the message but not the type so
        # the UI doesn't leak Polars internals.
        elapsed = (time.perf_counter() - started) * 1000.0
        logger.warning(
            "query.runtime_error",
            expression=expression,
            elapsed_ms=elapsed,
            error=str(exc),
            attempt=attempt,
        )
        raise _QueryFailure(
            expression, f"execution failed: {exc}", elapsed_ms=elapsed,
        )

    elapsed = (time.perf_counter() - started) * 1000.0
    rendered = _render(expression, result, elapsed_ms=elapsed)
    rendered.attempts = attempt
    rendered.rewrites = list(rewrites)
    if rendered.error is not None:
        raise _QueryFailure(expression, rendered.error, elapsed_ms=elapsed)
    return rendered


def _eval_sandboxed(compiled: Any, df: pl.DataFrame) -> Any:
    """Evaluate a compiled expression with ``df`` and ``pl`` in scope.

    Built-ins are stripped via ``{"__builtins__": {}}`` so even if the
    AST whitelist were bypassed, the sandbox would still refuse access
    to ``open``, ``__import__``, ``eval``, etc.

    A ``LazyFrame`` result is collected here so the work stays inside
    the timeout-guarded thread.
    """
    globals_ = {
        "__builtins__": {},
        "pl": pl,
        "df": df,
    }
    locals_: dict[str, Any] = {}
    result = eval(compiled, globals_, locals_)  # noqa: S307 — sandboxed, see above
    if isinstance(result, pl.LazyFrame):
        result = result.collect()
    return result


_SCALAR_TYPES: tuple[type, ...] = (
    int, float, str, bool, type(None),
    _dt.date, _dt.datetime, _dt.time, _dt.timedelta, _decimal.Decimal,
)


def _render(expression: str, result: Any, *, elapsed_ms: float) -> QueryResult:
    """Convert the eval output into a :class:`QueryResult`.

    Handles the result shapes that real Polars chains produce:
      * ``pl.DataFrame`` — normal case; pass through with truncation.
      * ``pl.Series``    — wrap as a single-column DataFrame.
      * scalar (int / float / str / bool / None / date / Decimal …) — e.g.
        from ``df.select(...).item()`` or ``df["x"].sum()``; wrap as
        one-row, one-col.
      * list / tuple of scalars — e.g. ``df.shape`` or ``df.columns``;
        wrap as a single ``result`` column.
      * dict — e.g. ``df.to_dict(as_series=False)`` or ``df.schema``;
        wrap as a DataFrame when the shape allows.

    Anything else is rejected as an error rather than silently dropped so
    the user gets a real failure message (and the repair round a chance
    to fix it).
    """
    df: pl.DataFrame
    try:
        if isinstance(result, pl.DataFrame):
            df = result
        elif isinstance(result, pl.Series):
            df = result.to_frame()
        elif isinstance(result, _SCALAR_TYPES):
            df = pl.DataFrame({"result": [result]})
        elif isinstance(result, (list, tuple)) and all(
            isinstance(item, _SCALAR_TYPES) for item in result
        ):
            df = pl.Series("result", list(result), strict=False).to_frame()
        elif isinstance(result, dict):
            if all(isinstance(v, (list, pl.Series)) for v in result.values()):
                df = pl.DataFrame(result)
            else:
                df = pl.DataFrame(
                    {"key": list(result.keys()),
                     "value": [str(v) for v in result.values()]}
                )
        else:
            return QueryResult(
                expression=expression,
                elapsed_ms=elapsed_ms,
                error=(
                    f"unsupported result type: {type(result).__name__}; "
                    "the expression must evaluate to a DataFrame, Series or scalar"
                ),
            )
    except Exception as exc:  # defensive: odd shapes from Polars constructors
        return QueryResult(
            expression=expression,
            elapsed_ms=elapsed_ms,
            error=f"could not render result: {exc}",
        )

    total_rows = df.height
    truncated = total_rows > QUERY_ROW_LIMIT
    if truncated:
        df = df.head(QUERY_ROW_LIMIT)

    # ``.rows()`` produces a list of tuples of native Python values which
    # round-trip cleanly through JSON. Cast tuples → lists so Pydantic
    # serialises them as JSON arrays.
    cells: list[list[Any]] = [list(row) for row in df.rows()]

    columns = [
        QueryColumn(name=col, dtype=str(dt))
        for col, dt in zip(df.columns, df.dtypes)
    ]

    return QueryResult(
        expression=expression,
        columns=columns,
        rows=cells,
        row_count=total_rows,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )


# ── helpers used by the chat agent ──────────────────────────────────────────

def schema_snapshot_from_profile(profile_columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the schema list the codegen prompt expects.

    Accepts a list of column dicts in the shape the chat agent already
    has (``name``, ``dtype`` from the ProfileReport, optional
    ``semantic_type``) and copies through only the fields the prompt
    actually needs. Keeps the prompt token count tight.
    """
    pared: list[dict[str, Any]] = []
    for col in profile_columns:
        if col.get("schema_truncated"):
            pared.append({
                "schema_truncated": True,
                "included_column_count": col["included_column_count"],
                "total_column_count": col["total_column_count"],
            })
            continue
        entry: dict[str, Any] = {
            "name": col["name"],
            "dtype": col.get("dtype") or col.get("polars_dtype") or "Unknown",
        }
        sem = col.get("semantic_type")
        if sem:
            entry["semantic_type"] = sem
        pared.append(entry)
    return pared


__all__ = [
    "QueryValidationError",
    "execute_query",
    "schema_snapshot_from_profile",
    "validate_expression",
]


# A typed helper that callers can use to attach a fresh message_id to a
# QueryResult when persisting an assistant turn. Kept here so the
# import surface stays narrow.
def fresh_message_id() -> uuid.UUID:
    return uuid.uuid4()
