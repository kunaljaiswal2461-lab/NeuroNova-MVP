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
from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable
from agentic_engine.profiler.loader import UnreadableDatasetError, load_dataset


logger = get_logger("conversational.query")


# ── AST whitelist ────────────────────────────────────────────────────────────

# Only these AST node *types* may appear anywhere in the expression. Any
# other node type causes validation to fail. Default-deny is the key
# property here — we never trust the model to stay on the path; the AST
# is the enforcement boundary.
_ALLOWED_NODE_TYPES: frozenset[type[ast.AST]] = frozenset({
    ast.Expression,
    ast.Call,
    ast.Attribute,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.Index,            # legacy slice in older Python versions
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.keyword,
    # Arithmetic / comparison / boolean operators — these are *operator*
    # nodes, not expressions; whitelisted explicitly so e.g. `**` (power,
    # could be costly with large floats) is not implicitly allowed.
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv,
    ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn,
    ast.And, ast.Or, ast.Not,
    ast.BitAnd, ast.BitOr, ast.BitXor, ast.Invert,
})

# Names that may appear as standalone identifiers. ``True`` / ``False`` /
# ``None`` are represented as ``ast.Constant`` post-3.8 so they don't
# need to be listed here.
_ALLOWED_NAMES: frozenset[str] = frozenset({"df", "pl"})

# Polars module-level functions exposed to the model. Anything not in
# this set called as ``pl.<x>(...)`` is rejected — this is the strongest
# guard, because the model could otherwise reach ``pl.read_csv`` or
# ``pl.scan_parquet`` to read arbitrary files.
_ALLOWED_PL_FUNCTIONS: frozenset[str] = frozenset({
    "col", "lit", "when",
    "sum", "mean", "median", "min", "max", "count", "n_unique",
    "first", "last",
    "all", "any",
    "concat_str", "concat_list",
    "int_range",
})

# Polars dtype names that may appear as ``pl.<DType>`` (e.g. for
# ``cast(pl.Float64)``). These are referenced as Attributes but not
# called, so we list them separately and accept them at Attribute time.
_ALLOWED_PL_DTYPES: frozenset[str] = frozenset({
    "Float64", "Float32",
    "Int64", "Int32", "Int16", "Int8",
    "UInt64", "UInt32", "UInt16", "UInt8",
    "Boolean", "Utf8", "String",
    "Date", "Datetime", "Time", "Duration",
    "List", "Array", "Struct", "Categorical", "Object", "Null",
})

# All identifiers permitted as ``pl.<x>`` regardless of whether they are
# called or just referenced.
_ALLOWED_PL_ATTRIBUTES: frozenset[str] = _ALLOWED_PL_FUNCTIONS | _ALLOWED_PL_DTYPES

# Methods that may be called on a DataFrame / Series / Expr. We do not
# distinguish between them in the AST — Polars chains are uniform — so
# the union is one set. The bias is read-only and small.
_ALLOWED_METHODS: frozenset[str] = frozenset({
    # DataFrame read-only ops
    "filter", "select", "with_columns", "sort", "head", "tail", "limit",
    "slice", "drop", "drop_nulls", "unique", "rename",
    "group_by", "groupby", "agg",
    "describe", "n_unique", "row_count",
    "to_dict",
    # DataFrame read-only properties
    "shape", "columns", "dtypes", "height", "width",
    # Aggregations (also valid on Series + Expr)
    "sum", "mean", "median", "min", "max", "count", "value_counts",
    "std", "var", "quantile",
    # Expression / column ops
    "alias", "cast", "is_null", "is_not_null", "is_in", "between",
    "gt", "lt", "ge", "le", "eq", "ne",
    "and_", "or_", "not_",
    "then", "otherwise",
    # Namespaces
    "str", "dt",
    # Common .str.* / .dt.* methods that are read-only
    "contains", "starts_with", "ends_with",
    "to_lowercase", "to_uppercase", "len_chars", "len_bytes",
    "year", "month", "day", "weekday", "hour", "minute", "second",
    "to_date", "to_datetime",
})


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

        if isinstance(node, ast.Subscript):
            # df["col"] is fine; df[some_expr] is rejected — we want
            # column names to be literal so the generated code is
            # auditable.
            slice_value = node.slice
            if isinstance(slice_value, ast.Index):  # py<3.9 compat
                slice_value = slice_value.value  # type: ignore[attr-defined]
            if isinstance(slice_value, ast.Slice):
                continue
            if not isinstance(slice_value, ast.Constant):
                raise QueryValidationError(
                    "subscript keys must be literal constants"
                )

    return tree


# ── execution ────────────────────────────────────────────────────────────────

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
    """
    settings = settings or get_settings()

    # ── 1. Codegen. ────────────────────────────────────────────────────
    if llm is None:
        return QueryResult(
            expression="",
            error="LLM client is not configured; cannot generate query",
        )

    try:
        codegen = await llm.chat_json(
            model=settings.openai_chat_model,
            system=query_codegen.SYSTEM,
            user=query_codegen.build_user(
                message,
                schema_snapshot,
                history=history,
            ),
            max_tokens=600,
            temperature=0.1,
        )
    except LLMUnavailable as exc:
        logger.warning("query.codegen_failed", error=str(exc))
        return QueryResult(
            expression="",
            error=f"codegen unavailable: {exc}",
        )

    expression = (codegen.content_json.get("expression") or "").strip()
    if not expression:
        return QueryResult(
            expression="",
            error="codegen returned no expression",
        )

    # ── 2. AST validation. ─────────────────────────────────────────────
    try:
        tree = validate_expression(expression)
    except QueryValidationError as exc:
        logger.warning(
            "query.rejected_by_ast",
            expression=expression,
            error=str(exc),
        )
        return QueryResult(
            expression=expression,
            error=f"rejected by AST validator: {exc}",
        )

    # ── 3. Load the dataset (always re-read; the executor is stateless). ─
    try:
        df = await asyncio.to_thread(load_dataset, raw_path, file_type)
    except UnreadableDatasetError as exc:
        logger.warning("query.unreadable_dataset", error=str(exc))
        return QueryResult(
            expression=expression,
            error=f"could not load dataset: {exc}",
        )

    # ── 4. Compile + sandboxed eval inside a thread with a timeout. ────
    try:
        compiled = compile(tree, filename="<nl-query>", mode="eval")
    except (SyntaxError, ValueError) as exc:
        # Belt-and-braces — validate_expression caught most of this, but
        # compile() can still reject (e.g. recursion depth).
        return QueryResult(
            expression=expression,
            error=f"compile failed: {exc}",
        )

    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_eval_sandboxed, compiled, df),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        elapsed = (time.perf_counter() - started) * 1000.0
        logger.warning(
            "query.timeout",
            expression=expression,
            elapsed_ms=elapsed,
        )
        return QueryResult(
            expression=expression,
            elapsed_ms=elapsed,
            error=f"query exceeded {QUERY_TIMEOUT_SECONDS}s timeout",
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
        )
        return QueryResult(
            expression=expression,
            elapsed_ms=elapsed,
            error=f"execution failed: {exc}",
        )

    elapsed = (time.perf_counter() - started) * 1000.0

    # ── 5. Coerce to (columns, rows). ──────────────────────────────────
    return _render(expression, result, elapsed_ms=elapsed)


def _eval_sandboxed(compiled: Any, df: pl.DataFrame) -> Any:
    """Evaluate a compiled expression with ``df`` and ``pl`` in scope.

    Built-ins are stripped via ``{"__builtins__": {}}`` so even if the
    AST whitelist were bypassed, the sandbox would still refuse access
    to ``open``, ``__import__``, ``eval``, etc.
    """
    globals_ = {
        "__builtins__": {},
        "pl": pl,
        "df": df,
    }
    locals_: dict[str, Any] = {}
    return eval(compiled, globals_, locals_)  # noqa: S307 — sandboxed, see above


def _render(expression: str, result: Any, *, elapsed_ms: float) -> QueryResult:
    """Convert the eval output into a :class:`QueryResult`.

    Handles three result shapes that real Polars chains can produce:
      * ``pl.DataFrame`` — normal case; pass through with truncation.
      * ``pl.Series``    — wrap as a single-column DataFrame.
      * scalar (int / float / str / bool) — wrap as one-row, one-col.

    Anything else (e.g. a tuple) is rejected as an error rather than
    silently dropped so the user gets a real failure message.
    """
    df: pl.DataFrame
    if isinstance(result, pl.DataFrame):
        df = result
    elif isinstance(result, pl.Series):
        df = result.to_frame()
    elif isinstance(result, (int, float, str, bool)):
        df = pl.DataFrame({"result": [result]})
    else:
        return QueryResult(
            expression=expression,
            elapsed_ms=elapsed_ms,
            error=f"unsupported result type: {type(result).__name__}",
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
