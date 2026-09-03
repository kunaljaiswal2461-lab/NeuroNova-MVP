"""Single source of truth for the NL→Polars sandbox grammar.

Both the AST validator (:mod:`query_executor`) and the code-generation
prompt (:mod:`prompts.query_codegen`) import from here, so the model is
only ever told about methods the validator will actually accept. The
two previously drifted apart (the prompt advertised ``.between`` while
Polars 1.x only has ``.is_between``), which produced confident
generations that were rejected at validation time.

Every name below is a read-only operation verified against the pinned
Polars release in ``requirements.txt``. Nothing here performs I/O,
accepts a callable, or reaches outside the ``df`` / ``pl`` namespace.
"""
from __future__ import annotations

import ast

POLARS_API_VERSION = "1.x"

# ── AST node types ───────────────────────────────────────────────────────────

ALLOWED_NODE_TYPES: frozenset[type[ast.AST]] = frozenset({
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
    # Operator nodes are whitelisted explicitly so e.g. ``**`` (power,
    # potentially expensive on large floats) is not implicitly allowed.
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv,
    ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn,
    ast.And, ast.Or, ast.Not,
    ast.BitAnd, ast.BitOr, ast.BitXor, ast.Invert,
})

# Names that may appear as standalone identifiers.
ALLOWED_NAMES: frozenset[str] = frozenset({"df", "pl"})

# ── ``pl.<x>`` module-level surface ──────────────────────────────────────────

ALLOWED_PL_FUNCTIONS: frozenset[str] = frozenset({
    "col", "lit", "when",
    "sum", "mean", "median", "min", "max", "len", "count", "n_unique",
    "std", "var", "quantile", "corr", "cov",
    "first", "last", "nth",
    "all", "any", "exclude",
    "concat_str", "concat_list", "concat",
    "sum_horizontal", "min_horizontal", "max_horizontal", "mean_horizontal",
    "coalesce", "struct", "format",
    "date", "datetime", "duration",
    "int_range",
})

ALLOWED_PL_DTYPES: frozenset[str] = frozenset({
    "Float64", "Float32",
    "Int64", "Int32", "Int16", "Int8",
    "UInt64", "UInt32", "UInt16", "UInt8",
    "Boolean", "Utf8", "String", "Decimal",
    "Date", "Datetime", "Time", "Duration",
    "List", "Array", "Struct", "Categorical", "Object", "Null",
})

ALLOWED_PL_ATTRIBUTES: frozenset[str] = ALLOWED_PL_FUNCTIONS | ALLOWED_PL_DTYPES

# ── methods / attributes on DataFrame, Series and Expr ───────────────────────

DATAFRAME_METHODS: frozenset[str] = frozenset({
    "filter", "select", "with_columns", "sort", "head", "tail", "limit",
    "slice", "drop", "drop_nulls", "unique", "rename", "reverse",
    "group_by", "agg", "pivot", "unpivot", "melt", "join",
    "top_k", "bottom_k", "sample",
    "describe", "null_count", "n_unique", "is_empty",
    "get_column", "to_series", "item", "to_dict",
    "estimated_size",
})

DATAFRAME_PROPERTIES: frozenset[str] = frozenset({
    "shape", "columns", "height", "width", "schema",
})

AGGREGATIONS: frozenset[str] = frozenset({
    "sum", "mean", "median", "min", "max", "count", "len", "value_counts",
    "std", "var", "quantile", "mode", "skew", "kurtosis", "product",
    "first", "last", "n_unique", "null_count", "unique_counts",
    "arg_max", "arg_min", "arg_sort",
})

EXPRESSION_METHODS: frozenset[str] = frozenset({
    "alias", "cast", "over", "sort_by", "rank", "exclude",
    "is_null", "is_not_null", "is_in", "is_between",
    "is_nan", "is_not_nan", "is_finite", "is_infinite",
    "is_duplicated", "is_unique", "is_first_distinct",
    "gt", "lt", "ge", "le", "eq", "ne",
    "and_", "or_", "not_", "xor",
    "then", "otherwise",
    "fill_null", "fill_nan", "round", "abs", "sqrt", "log", "exp",
    "floor", "ceil", "clip", "shift", "diff", "pct_change",
    "cum_sum", "cum_max", "cum_min", "cum_count",
    "explode", "implode", "flatten",
    # ``.name.suffix(...)`` / ``.name.prefix(...)`` / ``.name.keep()``
    "name", "suffix", "prefix", "keep",
    # Struct field access
    "struct", "field",
})

NAMESPACES: frozenset[str] = frozenset({"str", "dt"})

STRING_METHODS: frozenset[str] = frozenset({
    "contains", "contains_any", "starts_with", "ends_with",
    "to_lowercase", "to_uppercase", "to_titlecase",
    "len_chars", "len_bytes",
    "strip_chars", "strip_chars_start", "strip_chars_end",
    "replace", "replace_all", "split", "extract", "count_matches",
    "zfill", "pad_start", "pad_end",
    "to_date", "to_datetime", "strptime",
})

DATETIME_METHODS: frozenset[str] = frozenset({
    "year", "quarter", "month", "week", "weekday", "day", "ordinal_day",
    "hour", "minute", "second", "date", "truncate", "strftime", "to_string",
    "total_days", "total_hours", "total_minutes", "total_seconds",
    "epoch", "offset_by", "iso_year",
})

ALLOWED_METHODS: frozenset[str] = (
    DATAFRAME_METHODS
    | DATAFRAME_PROPERTIES
    | AGGREGATIONS
    | EXPRESSION_METHODS
    | NAMESPACES
    | STRING_METHODS
    | DATETIME_METHODS
)

# ── legacy → current API drift ───────────────────────────────────────────────
#
# The model is trained on years of Polars releases and periodically emits
# names from the pre-1.0 API. Rather than reject an otherwise correct
# query, the executor rewrites these deterministically at the AST level
# before validation. Everything here is a pure rename with identical
# semantics in Polars 1.x.

LEGACY_METHOD_RENAMES: dict[str, str] = {
    "groupby": "group_by",
    "with_column": "with_columns",
    "between": "is_between",
    "cumsum": "cum_sum",
    "cummax": "cum_max",
    "cummin": "cum_min",
    "cumcount": "cum_count",
    "is_first": "is_first_distinct",
    "lengths": "len_chars",
    "n_chars": "len_chars",
    "strip": "strip_chars",
    "lstrip": "strip_chars_start",
    "rstrip": "strip_chars_end",
    "days": "total_days",
    "hours": "total_hours",
    "minutes": "total_minutes",
    "seconds": "total_seconds",
}

# ``reverse=`` was renamed ``descending=`` across the sort family.
# NOTE: ``DataFrame.top_k`` / ``bottom_k`` still take ``reverse=`` in
# Polars 1.x (there ``descending`` is the *deprecated* alias), so they are
# deliberately absent from this table.
LEGACY_KEYWORD_RENAMES: dict[str, dict[str, str]] = {
    "sort": {"reverse": "descending"},
    "sort_by": {"reverse": "descending"},
    "arg_sort": {"reverse": "descending"},
    "rank": {"reverse": "descending"},
}

# Keyword *values* that make an otherwise read-only method a trivial
# memory/CPU amplifier. ``method -> {keyword: forbidden_value}``.
FORBIDDEN_KEYWORD_VALUES: dict[str, dict[str, object]] = {
    "join": {"how": "cross"},
    "sample": {"with_replacement": True},
}

# ``pl.count()`` (no args) is deprecated in favour of ``pl.len()``.
LEGACY_PL_FUNCTION_RENAMES: dict[str, str] = {
    "count": "len",
}
