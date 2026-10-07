"""Grader self-test (SPEC-03 §13.3).

A grader that passes everything is useless, and one that is never tested may well be one. For each
reference SQL this module generates, with sqlglot, two groups of variants:

* **equivalent rewrites**, which must grade PASS (a failure means the grader rejected a correct
  answer, i.e. a grader bug): reorder SELECT columns, rename every alias, add one extra column,
  wrap in a CTE, shuffle row order (only when order is not asked for);
* **wrong mutations**, which must grade FAIL: drop one WHERE predicate, flip ``>``/``>=`` and
  ``<``/``<=``, ``SUM``→``AVG``, ``COUNT(*)``→``COUNT(col)``, ``INNER``↔``LEFT JOIN``, flip
  ``ASC``/``DESC`` (only when order matters), ``LIMIT n``→``n-1``, change a string literal's case.

Generation is deliberately conservative: a rewrite that cannot be applied safely is skipped rather
than risking an invalid query. A query that nevertheless fails to execute is reported as *invalid*
and excluded from both rates.

A wrong mutant that still grades PASS is a *survivor*: either the grader is too lenient, or the
question's data cannot tell right from wrong (``data_insensitive``: the raw rows are identical).
Every survivor is listed. Accept one explicitly by putting ``accept-survivor:<kind>`` in the
question's ``notes``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Literal

import sqlglot
from sqlglot import exp

from evals.candidates.base import CandidateOutcome
from evals.grader import PASSING, ResultColumn, ResultSet, grade
from evals.models import Expectation, GoldenQuestion

# Fixture-specific knowledge (SPEC-03 §9.1). The harness owns the fixture, so this is safe to hard-code.
PRIMARY_KEY = {
    "regions": "id", "customers": "id", "products": "id", "orders": "id",
    "refunds": "id", "Order Events": "id", "page_views": "id", "order_items": "order_id",
}
NULLABLE_COLUMN = {
    "orders": "customer_id", "customers": "phone", "refunds": "reason", "page_views": "customer_id",
}
MAX_PER_KIND = 3


@dataclass(frozen=True)
class Mutant:
    kind: str
    group: Literal["equivalent", "wrong"]
    sql: str | None = None                 # None for result-level transforms
    transform: Literal["reverse_rows"] | None = None
    detail: str = ""


def _parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="postgres")


def _render(tree: exp.Expression) -> str:
    return tree.sql(dialect="postgres")


def _selects_star(tree: exp.Select) -> bool:
    """True for ``*`` / ``t.*`` in the select list (not for the star inside ``COUNT(*)``)."""
    return any(
        isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
        for e in tree.expressions
    )


def _has_ordinals(tree: exp.Select) -> bool:
    for key in ("order", "group"):
        clause = tree.args.get(key)
        if clause is None:
            continue
        for node in clause.expressions:
            target = node.this if isinstance(node, exp.Ordered) else node
            if isinstance(target, exp.Literal) and target.is_int:
                return True
    return False


def _main_table(tree: exp.Select) -> exp.Table | None:
    source = tree.args.get("from_")
    table = source.this if source is not None else None
    return table if isinstance(table, exp.Table) else None


def _split_and(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _split_and(node.left) + _split_and(node.right)
    if isinstance(node, exp.Paren) and isinstance(node.this, exp.And):
        return _split_and(node.this)
    return [node]


def _evenly(items: list, limit: int = MAX_PER_KIND) -> list[tuple[int, object]]:
    """Up to ``limit`` ``(index, item)`` pairs spread across the list, deterministically."""
    if len(items) <= limit:
        return list(enumerate(items))
    step = len(items) / limit
    picks = sorted({int(i * step) for i in range(limit)})
    return [(i, items[i]) for i in picks]


# ── equivalent rewrites ──────────────────────────────────────────────────────

def _reorder_columns(tree: exp.Expression) -> str | None:
    if not isinstance(tree, exp.Select) or len(tree.expressions) < 2:
        return None
    if _selects_star(tree) or _has_ordinals(tree):
        return None
    new = tree.copy()
    new.set("expressions", list(reversed(new.expressions)))
    return _render(new)


def _rename_aliases(tree: exp.Expression) -> str | None:
    if not isinstance(tree, exp.Select) or _selects_star(tree) or _has_ordinals(tree):
        return None
    new = tree.copy()
    mapping: dict[str, str] = {}
    renamed = []
    for i, e in enumerate(new.expressions):
        target = f"m{i + 1}"
        if isinstance(e, exp.Alias):
            mapping[e.alias.lower()] = target
            renamed.append(exp.alias_(e.this, target))
        else:
            renamed.append(exp.alias_(e, target))
    new.set("expressions", renamed)
    for key in ("order", "group", "having"):
        clause = new.args.get(key)
        if clause is None:
            continue
        for col in list(clause.find_all(exp.Column)):
            if not col.table and col.name.lower() in mapping:
                col.replace(exp.column(mapping[col.name.lower()]))
    return _render(new)


def _add_extra_column(tree: exp.Expression) -> str | None:
    if not isinstance(tree, exp.Select) or tree.args.get("distinct") or tree.args.get("group"):
        return None
    if tree.find(exp.AggFunc) or tree.args.get("having") or _has_ordinals(tree):
        return None
    table = _main_table(tree)
    if table is None or table.name not in PRIMARY_KEY:
        return None
    new = tree.copy()
    qualifier = _main_table(new).alias_or_name
    new.set("expressions", list(new.expressions) + [exp.column(PRIMARY_KEY[table.name], table=qualifier)])
    return _render(new)


def _wrap_cte(tree: exp.Expression) -> str:
    return _render(exp.select("*").from_("__ref").with_("__ref", as_=tree.copy()))


# ── wrong mutations ──────────────────────────────────────────────────────────

def _drop_predicate(tree: exp.Expression) -> Iterable[tuple[str, str]]:
    where = tree.args.get("where") if isinstance(tree, exp.Select) else None
    if where is None:
        return
    parts = _split_and(where.this)
    for i, part in _evenly(parts):
        new = tree.copy()
        rest = [p.copy() for j, p in enumerate(parts) if j != i]
        new.set("where", exp.Where(this=exp.and_(*rest)) if rest else None)
        yield f"dropped `{part.sql(dialect='postgres')[:60]}`", _render(new)


_FLIPS: dict[type, type] = {exp.GT: exp.GTE, exp.GTE: exp.GT, exp.LT: exp.LTE, exp.LTE: exp.LT}


def _flip_comparison(tree: exp.Expression) -> Iterable[tuple[str, str]]:
    nodes = [n for n in tree.walk() if isinstance(n, tuple(_FLIPS))]
    for i, node in _evenly(nodes):
        new = tree.copy()
        target = [n for n in new.walk() if isinstance(n, tuple(_FLIPS))][i]
        replacement = _FLIPS[type(target)](this=target.this.copy(), expression=target.expression.copy())
        target.replace(replacement)
        yield f"{type(node).__name__} -> {_FLIPS[type(node)].__name__}", _render(new)


def _agg_swap(tree: exp.Expression) -> Iterable[tuple[str, str]]:
    swaps = {exp.Sum: exp.Avg, exp.Avg: exp.Sum}
    nodes = [n for n in tree.walk() if isinstance(n, tuple(swaps))]
    for i, node in _evenly(nodes):
        new = tree.copy()
        target = [n for n in new.walk() if isinstance(n, tuple(swaps))][i]
        target.replace(swaps[type(target)](this=target.this.copy()))
        yield f"{type(node).__name__.upper()} -> {swaps[type(node)].__name__.upper()}", _render(new)
    table = _main_table(tree) if isinstance(tree, exp.Select) else None
    column = NULLABLE_COLUMN.get(table.name) if table is not None else None
    if column:
        def top_level_stars(root: exp.Expression) -> list[exp.Count]:
            # Only COUNT(*) that belongs to the outermost SELECT: inside a subquery the main table differs.
            return [
                n for n in root.walk()
                if isinstance(n, exp.Count) and isinstance(n.this, exp.Star) and n.find_ancestor(exp.Select) is root
            ]

        for i, _ in _evenly(top_level_stars(tree), 1):
            new = tree.copy()
            target = top_level_stars(new)[i]
            target.replace(exp.Count(this=exp.column(column, table=_main_table(new).alias_or_name)))
            yield f"COUNT(*) -> COUNT({column})", _render(new)


# Foreign-key columns that may be NULL in the fixture (guest orders, anonymous page views). For every
# other join, INNER vs LEFT cannot differ: the key is NOT NULL and referentially intact, so flipping it
# would be an equivalent rewrite rather than a wrong mutation.
NULLABLE_FK_COLUMNS = frozenset({"customer_id"})


def _joins_on_nullable_fk(join: exp.Join) -> bool:
    on = join.args.get("on")
    return on is not None and any(c.name in NULLABLE_FK_COLUMNS for c in on.find_all(exp.Column))


def _join_flip(tree: exp.Expression) -> Iterable[tuple[str, str]]:
    joins = [n for n in tree.walk() if isinstance(n, exp.Join)]
    for i, join in _evenly(joins, 2):
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if side == "LEFT":
            change, label = {"side": None, "kind": "INNER"}, "LEFT -> INNER JOIN"
        elif not side and kind in ("", "INNER") and _joins_on_nullable_fk(join):
            change, label = {"side": "LEFT", "kind": None}, "INNER -> LEFT JOIN"
        else:
            continue
        new = tree.copy()
        target = [n for n in new.walk() if isinstance(n, exp.Join)][i]
        for key, value in change.items():
            target.set(key, value)
        yield label, _render(new)


def _flip_order(tree: exp.Expression) -> Iterable[tuple[str, str]]:
    order = tree.args.get("order")
    if order is None:
        return
    new = tree.copy()
    for ordered in new.args["order"].expressions:
        if isinstance(ordered, exp.Ordered):
            ordered.set("desc", not ordered.args.get("desc"))
            ordered.set("nulls_first", None)
    yield "ASC/DESC flipped", _render(new)


def _limit_minus_one(tree: exp.Expression) -> Iterable[tuple[str, str]]:
    limit = tree.args.get("limit")
    try:
        n = int(limit.expression.name) if limit is not None else 0
    except (ValueError, AttributeError):
        return
    if n > 1:
        new = tree.copy()
        new.set("limit", exp.Limit(expression=exp.Literal.number(n - 1)))
        yield f"LIMIT {n} -> {n - 1}", _render(new)


def _comparison_literals(tree: exp.Expression) -> list[exp.Literal]:
    """String literals that are compared with ``=``, ``<>`` or ``IN`` (not function arguments).

    Case-flipping ``'Asia/Kolkata'`` or a ``date_trunc`` unit would be an *equivalent* rewrite
    (both are case-insensitive in PostgreSQL), so only value comparisons are mutated.
    """
    return [
        n
        for n in tree.walk()
        if isinstance(n, exp.Literal)
        and n.is_string
        and re.search(r"[A-Za-z]", n.name)
        and isinstance(n.parent, (exp.EQ, exp.NEQ, exp.In))
    ]


def _string_case(tree: exp.Expression) -> Iterable[tuple[str, str]]:
    literals = _comparison_literals(tree)
    for i, lit in _evenly(literals, 2):
        new = tree.copy()
        target = _comparison_literals(new)[i]
        target.replace(exp.Literal.string(target.name.swapcase()))
        yield f"'{lit.name}' -> '{lit.name.swapcase()}'", _render(new)


def generate_mutants(sql: str, *, order_matters: bool) -> list[Mutant]:
    tree = _parse(sql)
    original = _render(tree)
    out: list[Mutant] = []
    seen = {original}

    def add(kind: str, group: Literal["equivalent", "wrong"], new_sql: str | None, detail: str = "") -> None:
        if new_sql is None or new_sql in seen:
            return
        seen.add(new_sql)
        out.append(Mutant(kind, group, sql=new_sql, detail=detail))

    add("reorder_columns", "equivalent", _reorder_columns(tree))
    add("rename_aliases", "equivalent", _rename_aliases(tree))
    add("add_extra_column", "equivalent", _add_extra_column(tree))
    if not order_matters:
        add("wrap_cte", "equivalent", _wrap_cte(tree))
        out.append(Mutant("shuffle_rows", "equivalent", transform="reverse_rows", detail="row order reversed"))

    for kind, fn, only_when_ordered in (
        ("drop_predicate", _drop_predicate, False),
        ("flip_comparison", _flip_comparison, False),
        ("agg_swap", _agg_swap, False),
        ("join_flip", _join_flip, False),
        ("flip_order", _flip_order, True),
        ("limit_minus_one", _limit_minus_one, False),
        ("string_case", _string_case, False),
    ):
        if only_when_ordered and not order_matters:
            continue
        for detail, new_sql in fn(tree):
            add(kind, "wrong", new_sql, detail)
    return out


# ── evaluation ───────────────────────────────────────────────────────────────

@dataclass
class MutantResult:
    question_id: str
    kind: str
    group: str
    status: Literal["pass", "fail", "invalid"]
    verdict: str
    sql: str | None
    detail: str
    survivor_class: Literal["data_insensitive", "grader_lenient", "accepted"] | None = None
    tags: tuple[str, ...] = ()


@dataclass
class MutantSummary:
    results: list[MutantResult] = field(default_factory=list)

    def _of(self, group: str, *statuses: str) -> list[MutantResult]:
        return [r for r in self.results if r.group == group and r.status in statuses]

    @property
    def equivalence_pass_rate(self) -> float | None:
        valid = self._of("equivalent", "pass", "fail")
        return len(self._of("equivalent", "pass")) / len(valid) if valid else None

    @property
    def survivors(self) -> list[MutantResult]:
        return [r for r in self._of("wrong", "pass") if r.survivor_class != "accepted"]

    @property
    def accepted_survivors(self) -> list[MutantResult]:
        return [r for r in self._of("wrong", "pass") if r.survivor_class == "accepted"]

    @property
    def mutation_kill_rate(self) -> float | None:
        killed = len(self._of("wrong", "fail"))
        denominator = killed + len(self.survivors)
        return killed / denominator if denominator else None

    def extras(self) -> dict:
        return {
            "equivalence_total": len(self._of("equivalent", "pass", "fail")),
            "equivalence_failed": len(self._of("equivalent", "fail")),
            "equivalence_pass_rate": self.equivalence_pass_rate,
            "wrong_total": len(self._of("wrong", "pass", "fail")),
            "wrong_killed": len(self._of("wrong", "fail")),
            "survivors": len(self.survivors),
            "survivors_data_insensitive": sum(r.survivor_class == "data_insensitive" for r in self.survivors),
            "survivors_grader_lenient": sum(r.survivor_class == "grader_lenient" for r in self.survivors),
            "accepted_survivors": len(self.accepted_survivors),
            "invalid_mutants": len(self._of("equivalent", "invalid")) + len(self._of("wrong", "invalid")),
            "mutation_kill_rate": self.mutation_kill_rate,
        }


RunResult = Callable[[str], Awaitable[ResultSet]]


def _multiset(result: ResultSet) -> list[str]:
    return sorted(repr(r) for r in result.rows)


def _accepted(question: GoldenQuestion, kind: str) -> bool:
    return f"accept-survivor:{kind}" in (question.notes or "")


async def evaluate_question(
    question: GoldenQuestion, reference: ResultSet, run: RunResult
) -> list[MutantResult]:
    expect: Expectation = question.expect
    results: list[MutantResult] = []
    for mutant in generate_mutants(expect.reference_sql, order_matters=expect.order_matters):
        try:
            if mutant.transform == "reverse_rows":
                candidate = ResultSet(reference.columns, tuple(reversed(reference.rows)), reference.complete)
            else:
                candidate = await run(mutant.sql)
        except Exception as exc:  # noqa: BLE001 - an invalid mutant is a generator limitation, not a grader result
            results.append(
                MutantResult(question.id, mutant.kind, mutant.group, "invalid", f"error:{type(exc).__name__}", mutant.sql, mutant.detail, tags=question.tags)
            )
            continue
        g = grade(expect, reference, CandidateOutcome(kind="answer", result=candidate, sql=mutant.sql))
        passed = g.verdict in PASSING
        status: Literal["pass", "fail"] = "pass" if passed else "fail"
        survivor_class = None
        if mutant.group == "wrong" and passed:
            if _accepted(question, mutant.kind):
                survivor_class = "accepted"
            elif _multiset(candidate) == _multiset(reference):
                survivor_class = "data_insensitive"
            else:
                survivor_class = "grader_lenient"
        results.append(
            MutantResult(question.id, mutant.kind, mutant.group, status, g.verdict.value, mutant.sql, mutant.detail, survivor_class, question.tags)
        )
    return results
