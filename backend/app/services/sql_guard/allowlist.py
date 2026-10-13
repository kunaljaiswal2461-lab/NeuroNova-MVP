"""Allowlists for the SQL validator (SPEC-04 §8).

Everything is allowlisted, nothing is denylisted: whatever has not been reviewed
is rejected. Adding an entry later needs a test that uses it plus a live
round-trip test (T-L1) covering it. Do not widen these to make a query pass;
that is an owner decision (SPEC-04 §13.3).
"""
from __future__ import annotations

from sqlglot import exp

# §8.1 sqlglot node classes ---------------------------------------------------

_STRUCTURE = """Select Union Intersect Except With CTE Subquery From Join Lateral Where Group Having Order
Ordered Limit Offset Fetch Distinct Window WindowSpec Table TableAlias Alias Column Identifier Star Paren
Tuple Rollup Cube GroupingSets Exists Filter WithinGroup"""
_LITERALS = "Literal Boolean Null Var DataType DataTypeParam Interval RawString UnicodeString"
_LOGIC = """And Or Not EQ NEQ GT GTE LT LTE NullSafeEQ NullSafeNEQ Is In Between Case If Like ILike
SimilarTo RegexpLike RegexpILike"""
_ARITH = "Add Sub Mul Div Mod Neg DPipe Cast TryCast AtTimeZone"
_AGG = """Count Sum Avg Min Max Stddev StddevSamp StddevPop Variance VariancePop GroupConcat
PercentileCont PercentileDisc Mode LogicalAnd LogicalOr"""
_WINDOW = "RowNumber Rank DenseRank PercentRank CumeDist Ntile Lag Lead FirstValue LastValue NthValue"
_SCALAR = """Abs Round Ceil Floor Trunc Pow Sqrt Exp Ln Log Sign Greatest Least Coalesce Nullif Lower
Upper Trim Length Substring Left Right Replace Concat ConcatWs SplitPart Initcap Pad StrPosition
TimestampTrunc Extract TimeToStr StrToDate StrToTime CurrentDate CurrentTimestamp Anonymous"""
_TABLE_FUNCTIONS = "ExplodingGenerateSeries GenerateSeries"

ALLOWED_NODE_TYPES: frozenset[str] = frozenset(
    " ".join((_STRUCTURE, _LITERALS, _LOGIC, _ARITH, _AGG, _WINDOW, _SCALAR, _TABLE_FUNCTIONS)).split()
)

# Human wording for the classes most likely to appear in a rejection message.
NODE_DESCRIPTIONS: dict[str, str] = {
    "Into": "SELECT INTO", "Lock": "FOR UPDATE/SHARE", "Command": "this statement type",
    "TableSample": "TABLESAMPLE", "Values": "VALUES lists", "Array": "arrays",
    "Any": "ANY/ALL (use IN (...) instead)", "All": "ANY/ALL (use IN (...) instead)",
    "ArrayAgg": "array_agg", "Delete": "data-modifying statements", "Insert": "data-modifying statements",
    "Update": "data-modifying statements", "Merge": "data-modifying statements",
    "Parameter": "query parameters", "Placeholder": "query parameters", "BitwiseAnd": "bitwise operators",
    "CurrentUser": "current_user", "SessionUser": "session_user", "CurrentVersion": "version()",
    "CurrentDatabase": "current_database()", "Collate": "COLLATE",
}

# §8.2 Postgres function names (token layer, lowercase) ------------------------

ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    """count sum avg min max stddev stddev_samp stddev_pop variance var_samp var_pop string_agg
percentile_cont percentile_disc mode bool_and bool_or row_number rank dense_rank percent_rank cume_dist
ntile lag lead first_value last_value nth_value abs round ceil ceiling floor trunc mod power pow sqrt
exp ln log sign greatest least coalesce nullif lower upper trim btrim ltrim rtrim length char_length
substring substr left right replace concat concat_ws split_part initcap lpad rpad strpos position
date_trunc date_part extract to_char to_date to_timestamp now age make_date generate_series cast""".split()
)

# Allowlisted names that are SQL syntax constructs with no pg_proc row (owner decision D2).
SYNTAX_CONSTRUCTS: frozenset[str] = frozenset({"cast", "coalesce", "greatest", "least", "nullif", "trim"})

# §8.3 cast target types (sqlglot DataType.Type names; ``numeric(12,2)`` etc. share the base type) -----

ALLOWED_TYPE_NAMES: frozenset[str] = frozenset(
    """SMALLINT INT BIGINT DECIMAL FLOAT DOUBLE TEXT VARCHAR CHAR BPCHAR BOOLEAN DATE TIMESTAMP
TIMESTAMPTZ INTERVAL UUID""".split()
)

# §8.4 table functions --------------------------------------------------------

ALLOWED_TABLE_FUNCTION_NODES: frozenset[str] = frozenset({"ExplodingGenerateSeries", "GenerateSeries"})

ROOT_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)

# Words Postgres reads as the SQL special value even when a column has that name: an unquoted,
# unqualified ``user`` is current_user, not the column. sqlglot may parse some of them as a plain Column.
SQL_VALUE_KEYWORDS: frozenset[str] = frozenset(
    """user current_user current_role session_user system_user current_catalog current_schema
current_date current_time current_timestamp localtime localtimestamp""".split()
)
