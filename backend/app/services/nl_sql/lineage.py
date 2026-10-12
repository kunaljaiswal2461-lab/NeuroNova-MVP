"""Which source columns does each output column read? (SPEC-09 W3.3 step 1)

Reads the validator's regenerated SQL. A projection that reads only plain catalog columns of the tables in FROM/JOIN
reports exactly those columns. Anything this function cannot follow (a star, a subquery, a CTE, a set operation, a
column of a derived table, a position mismatch) is reported as ``None`` = *unknown*, and the caller treats unknown as
personal. ``count(...)`` reads columns but returns a number that exposes none of them, so it reports no sources.
"""
from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

ColumnKey = tuple[str, str, str]
Sources = frozenset[ColumnKey] | None


class _Quiet(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:     # sqlglot logs the SQL text on errors
        return False


def _parse(sql: str) -> exp.Expression | None:
    quiet = _Quiet()
    log = logging.getLogger("sqlglot")
    log.addFilter(quiet)
    try:
        trees = sqlglot.parse(sql, read="postgres")
    except (SqlglotError, RecursionError):
        return None
    finally:
        log.removeFilter(quiet)
    return trees[0] if len(trees) == 1 and trees[0] is not None else None


def output_sources(sql: str, column_count: int) -> list[Sources]:
    unknown: list[Sources] = [None] * column_count
    tree = _parse(sql)
    if not isinstance(tree, exp.Select) or tree.args.get("with"):
        return unknown
    projections = list(tree.expressions)
    if len(projections) != column_count or any(isinstance(p.unalias(), exp.Star) or isinstance(p, exp.Star) for p in projections):
        return unknown

    # alias (or bare table name) -> (schema, table), or None for a derived table.
    scope: dict[str, tuple[str, str] | None] = {}
    sources = []
    from_clause = tree.args.get("from") or tree.args.get("from_")
    if from_clause is not None:
        sources.append(from_clause.this)
    for join in tree.args.get("joins") or []:
        sources.append(join.this)
    for source in sources:
        if isinstance(source, exp.Table) and source.db and not isinstance(source.this, exp.Func):
            scope[source.alias_or_name] = (source.db, source.name)
        elif isinstance(source, exp.Table):
            scope[source.alias_or_name] = None
        else:
            alias = getattr(source, "alias_or_name", None)
            if alias:
                scope[alias] = None
            else:
                return unknown
    real = [value for value in scope.values() if value is not None]

    result: list[Sources] = []
    for projection in projections:
        inner = projection.unalias()
        if isinstance(inner, exp.Count):
            result.append(frozenset())
            continue
        if inner.find(exp.Subquery, exp.Select, exp.Window) is not None or inner.find(exp.Star) is not None:
            result.append(None)
            continue
        found: set[ColumnKey] = set()
        known = True
        for column in inner.find_all(exp.Column):
            qualifier = column.table
            if qualifier:
                target = scope.get(qualifier)
            elif len(scope) == 1:
                target = real[0] if real else None
            else:
                target = None
            if target is None:
                known = False
                break
            found.add((target[0], target[1], column.name))
        result.append(frozenset(found) if known else None)
    return result
