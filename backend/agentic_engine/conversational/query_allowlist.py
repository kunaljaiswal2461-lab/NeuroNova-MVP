"""Minimal AST surface for deterministic QueryPlan compiler output.

The LLM never sees or generates Polars code. This allowlist is a final defense
for the small, backend-owned expression shape emitted by ``query_compiler``.
"""
from __future__ import annotations

import ast


POLARS_API_VERSION = "1.x"

ALLOWED_NODE_TYPES: frozenset[type[ast.AST]] = frozenset({
    ast.Expression,
    ast.Call,
    ast.Attribute,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.keyword,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.BitAnd,
    ast.Invert,
})

ALLOWED_NAMES: frozenset[str] = frozenset({"df", "pl"})

# Module-level functions used by the compiler. Every item must also have an
# explicit signature in app.core.config.POLARS_FUNCTION_SIGNATURES.
ALLOWED_PL_FUNCTIONS: frozenset[str] = frozenset({"col", "len", "lit", "when"})
ALLOWED_PL_DTYPES: frozenset[str] = frozenset()
ALLOWED_PL_ATTRIBUTES: frozenset[str] = ALLOWED_PL_FUNCTIONS | ALLOWED_PL_DTYPES

# ``lazy`` and ``collect`` receive an additional compiler-owned check in
# query_executor.validate_expression; they are never accepted for arbitrary
# source text.
DATAFRAME_METHODS: frozenset[str] = frozenset({
    "lazy", "filter", "group_by", "agg", "select", "with_columns",
    "drop", "sort", "limit", "collect",
})
DATAFRAME_PROPERTIES: frozenset[str] = frozenset({"height"})
AGGREGATIONS: frozenset[str] = frozenset({
    "n_unique", "mean", "sum", "min", "max", "median",
})
EXPRESSION_METHODS: frozenset[str] = frozenset({
    "alias", "str", "contains", "is_null", "is_not_null", "then", "otherwise",
})
NAMESPACES: frozenset[str] = frozenset({"str"})
STRING_METHODS: frozenset[str] = frozenset({"contains"})
DATETIME_METHODS: frozenset[str] = frozenset()

ALLOWED_METHODS: frozenset[str] = (
    DATAFRAME_METHODS
    | DATAFRAME_PROPERTIES
    | AGGREGATIONS
    | EXPRESSION_METHODS
    | NAMESPACES
    | STRING_METHODS
    | DATETIME_METHODS
)

# The deterministic compiler never emits resource-amplifying operations. The
# mapping remains part of the generic AST check for defense in depth.
FORBIDDEN_KEYWORD_VALUES: dict[str, dict[str, object]] = {}
