"""Clock rewrite (SPEC-08 B6): the model's wall-clock functions become literals of the reference time.

The reference SQL uses literal bounds, never ``now()``; the validator allows ``now()``, which would evaluate at the
real wall clock and silently give another answer. Parse failures pass the text through unchanged so the validator
rejects it with its own code.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

_TIMESTAMP_NODES = (exp.CurrentTimestamp,)
_TIMESTAMP_FUNCTIONS = frozenset({"transaction_timestamp", "statement_timestamp"})


class _Quiet(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:     # sqlglot logs the offending SQL text on errors
        return False


def rewrite_clock(sql: str, reference_time: datetime, time_zone: str) -> tuple[str, bool]:
    """Return ``(sql, changed)``. ``reference_time`` must be timezone-aware."""
    zone = ZoneInfo(time_zone)
    local = reference_time.astimezone(zone)
    timestamp_literal = local.isoformat()
    date_literal = local.date().isoformat()
    wall_literal = local.replace(tzinfo=None).isoformat()      # localtimestamp is a plain TIMESTAMP in PostgreSQL

    sqlglot_logger = logging.getLogger("sqlglot")
    quiet = _Quiet()
    sqlglot_logger.addFilter(quiet)
    try:
        trees = sqlglot.parse(sql, read="postgres")
    except (SqlglotError, RecursionError):
        return sql, False
    finally:
        sqlglot_logger.removeFilter(quiet)
    if len(trees) != 1 or trees[0] is None:
        return sql, False
    tree = trees[0]

    changed = False

    def replace(node: exp.Expression):
        nonlocal changed
        if isinstance(node, _TIMESTAMP_NODES) or (
            isinstance(node, exp.Anonymous)
            and isinstance(node.this, str)
            and node.this.lower() in _TIMESTAMP_FUNCTIONS
            and not node.expressions
        ):
            changed = True
            return exp.Cast(this=exp.Literal.string(timestamp_literal), to=exp.DataType.build("timestamptz"))
        if isinstance(node, exp.Localtimestamp):
            changed = True
            return exp.Cast(this=exp.Literal.string(wall_literal), to=exp.DataType.build("timestamp"))
        if isinstance(node, exp.CurrentDate):
            changed = True
            return exp.Cast(this=exp.Literal.string(date_literal), to=exp.DataType.build("date"))
        return node

    try:
        new_tree = tree.transform(replace)
        if not changed:
            return sql, False
        return new_tree.sql(dialect="postgres"), True
    except (SqlglotError, RecursionError):
        return sql, False
