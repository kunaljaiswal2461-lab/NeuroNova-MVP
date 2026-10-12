"""Golden-set linter (SPEC-03 §11.7).

Static rules L1–L7 need no database. Live rules LL1–LL5 (plus LL6, see below) run the reference SQL
as ``nn_grader`` (INV-P0-08) through an injected async ``run_sql`` callable, so
the rule logic is unit-testable without Docker.

    python -m evals.lint [--live] [--require-reviewed]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import sqlglot
import yaml
from pydantic import ValidationError
from sqlglot import exp

from evals.models import (
    GOLDEN_DIR,
    MANIFEST_PATH,
    GoldenMeta,
    GoldenQuestion,
    load_meta,
    load_questions_raw,
)

MAX_REFERENCE_ROWS = 200

# (columns, rows) for a statement. Implemented over asyncpg in evals.pgrun.
RunSql = Callable[[str], Awaitable[tuple[list[str], list[tuple]]]]

_CLOCK_NODES = (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime, exp.Localtimestamp, exp.Localtime)
_CLOCK_FUNCS = frozenset(
    {"now", "clock_timestamp", "statement_timestamp", "transaction_timestamp", "timeofday"}
)


@dataclass(frozen=True)
class LintError:
    rule: str
    where: str          # question id or file
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.where}: {self.message}"


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="postgres")


def clock_calls(sql: str) -> list[str]:
    """Names of clock-dependent constructs in ``sql`` (parsed, not regex)."""
    found = []
    for node in _parse(sql).walk():
        if isinstance(node, _CLOCK_NODES):
            found.append(type(node).__name__)
        elif isinstance(node, exp.Anonymous) and str(node.this).lower() in _CLOCK_FUNCS:
            found.append(str(node.this).lower())
    return found


def has_top_level_order_by(sql: str) -> bool:
    return _parse(sql).args.get("order") is not None


def reference_sqls(q: GoldenQuestion) -> list[tuple[str, str]]:
    out = []
    if q.expect.reference_sql:
        out.append((q.id, q.expect.reference_sql))
    for i, alt in enumerate(q.expect.acceptable_answers):
        out.append((f"{q.id}#acceptable[{i}]", alt.reference_sql))
    return out


# ── static rules ─────────────────────────────────────────────────────────────

def lint_static(
    golden_dir: Path = GOLDEN_DIR,
    manifest_path: Path = MANIFEST_PATH,
    *,
    require_reviewed: bool = False,
) -> tuple[list[LintError], list[GoldenQuestion]]:
    errors: list[LintError] = []
    questions: list[GoldenQuestion] = []

    try:
        meta: GoldenMeta | None = load_meta(golden_dir)
    except (OSError, ValidationError, yaml.YAMLError) as exc:
        errors.append(LintError("L1", "meta.yaml", f"invalid or missing: {exc}"))
        meta = None

    # L1: schema-valid, unique ids
    seen: Counter[str] = Counter()
    for sub in ("questions", "adversarial", "chains"):
        directory = golden_dir / sub
        if not directory.is_dir():
            continue
        try:
            raw_items = load_questions_raw(directory)
        except (ValueError, yaml.YAMLError) as exc:
            errors.append(LintError("L1", str(directory), str(exc)))
            continue
        for path, item in raw_items:
            label = f"{path.name}:{item.get('id', '?') if isinstance(item, dict) else '?'}"
            try:
                q = GoldenQuestion.model_validate(item)
            except ValidationError as exc:
                first = exc.errors()[0]
                errors.append(LintError("L1", label, f"{'.'.join(map(str, first['loc']))}: {first['msg']}"))
                continue
            questions.append(q)
            seen[q.id] += 1
    for qid, n in seen.items():
        if n > 1:
            errors.append(LintError("L1", qid, f"duplicate id ({n} occurrences)"))

    for q in questions:
        # L2: vocabulary
        if meta is not None:
            for tag in q.tags:
                if tag not in meta.tags:
                    errors.append(LintError("L2", q.id, f"unknown tag {tag!r}"))
            if q.expect.output_type and q.expect.output_type not in meta.output_types:
                errors.append(LintError("L2", q.id, f"unknown output_type {q.expect.output_type!r}"))

        # L3: shape per kind
        e = q.expect
        if e.kind == "answer" and not e.reference_sql:
            errors.append(LintError("L3", q.id, "answer question has no reference_sql"))
        if e.kind == "clarification" and e.reference_sql:
            errors.append(LintError("L3", q.id, "clarification question must not have a top-level reference_sql"))
        if e.kind == "refusal" and not e.category:
            errors.append(LintError("L3", q.id, "refusal question has no category"))

        # L4 / L5 on every reference
        for label, sql in reference_sqls(q):
            try:
                clocks = clock_calls(sql)
            except sqlglot.errors.SqlglotError as exc:
                errors.append(LintError("L4", label, f"reference SQL does not parse: {exc}"))
                continue
            if clocks:
                errors.append(LintError("L4", label, f"clock-dependent construct(s): {sorted(set(clocks))}"))

        if e.reference_sql and e.order_matters:
            try:
                if not has_top_level_order_by(e.reference_sql):
                    errors.append(LintError("L5", q.id, "order_matters is true but the reference has no top-level ORDER BY"))
            except sqlglot.errors.SqlglotError:
                pass  # already reported under L4
        for i, alt in enumerate(e.acceptable_answers):
            if alt.order_matters:
                try:
                    if not has_top_level_order_by(alt.reference_sql):
                        errors.append(LintError("L5", f"{q.id}#acceptable[{i}]", "order_matters is true but no top-level ORDER BY"))
                except sqlglot.errors.SqlglotError:
                    pass

        # L7: review sign-off
        if require_reviewed and not q.reviewed_by:
            errors.append(LintError("L7", q.id, "not reviewed (reviewed_by is empty)"))

    # L6: fixture version
    if meta is not None:
        try:
            manifest_version = json.loads(manifest_path.read_text())["fixture_version"]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            errors.append(LintError("L6", "manifest.json", f"cannot read fixture_version: {exc}"))
        else:
            if manifest_version != meta.fixture_version:
                errors.append(
                    LintError("L6", "meta.yaml", f"fixture_version {meta.fixture_version} != manifest {manifest_version}")
                )
    return errors, questions


# ── live rules ───────────────────────────────────────────────────────────────

def _order_key_probe(tree: exp.Select) -> tuple[exp.Select, list[int]]:
    """Copy of ``tree`` exposing every ORDER BY expression as an output column."""
    select_exprs = list(tree.expressions)
    aliases = {e.alias_or_name.lower(): i for i, e in enumerate(select_exprs) if e.alias_or_name}
    key_indexes: list[int] = []
    extra: list[exp.Expression] = []
    for ordered in tree.args["order"].expressions:
        key = ordered.this
        if isinstance(key, exp.Literal) and key.is_int:
            key_indexes.append(int(key.name) - 1)
        elif isinstance(key, exp.Column) and not key.table and key.name.lower() in aliases:
            key_indexes.append(aliases[key.name.lower()])
        else:
            alias = f"__ok{len(extra)}"
            extra.append(exp.alias_(key.copy(), alias))
            key_indexes.append(len(select_exprs) + len(extra) - 1)
    probe = tree.copy()
    probe.set("expressions", select_exprs + extra)
    return probe, key_indexes


def tie_probe_sql(sql: str) -> tuple[str, int, list[int]] | None:
    """Rewrite a top-level ``ORDER BY … LIMIT n`` query for the LL3 tie check.

    Returns ``(probe_sql, n, key_indexes)``: the probe uses ``LIMIT n+1`` and
    exposes every ORDER BY expression as an output column; ``key_indexes`` are
    those columns' positions. ``None`` when the query has no LIMIT/ORDER BY.
    """
    tree = _parse(sql)
    limit = tree.args.get("limit")
    order = tree.args.get("order")
    if limit is None or order is None or not isinstance(tree, exp.Select):
        return None
    try:
        n = int(limit.expression.name)
    except (ValueError, AttributeError):
        return None
    probe, key_indexes = _order_key_probe(tree)
    probe.set("limit", exp.Limit(expression=exp.Literal.number(n + 1)))
    return probe.sql(dialect="postgres"), n, key_indexes


def order_keys_probe_sql(sql: str) -> tuple[str, list[int]] | None:
    """Rewrite any top-level ``ORDER BY`` query so its sort keys are output columns (LL6)."""
    tree = _parse(sql)
    if tree.args.get("order") is None or not isinstance(tree, exp.Select):
        return None
    probe, key_indexes = _order_key_probe(tree)
    return probe.sql(dialect="postgres"), key_indexes


async def lint_live(questions: list[GoldenQuestion], run_sql: RunSql) -> list[LintError]:
    errors: list[LintError] = []
    for q in questions:
        for label, sql in reference_sqls(q):
            primary = label == q.id
            order_matters = q.expect.order_matters if primary else q.expect.acceptable_answers[
                int(label.split("[")[1].rstrip("]"))
            ].order_matters
            try:  # LL1
                columns, rows = await run_sql(sql)
            except Exception as exc:  # noqa: BLE001 - report any DB error as a lint failure
                errors.append(LintError("LL1", label, f"reference failed: {type(exc).__name__}: {str(exc)[:200]}"))
                continue
            try:  # LL2
                _, rows2 = await run_sql(sql)
                if rows != rows2 and (order_matters or sorted(map(repr, rows)) != sorted(map(repr, rows2))):
                    errors.append(LintError("LL2", label, "two runs gave different results"))
            except Exception as exc:  # noqa: BLE001
                errors.append(LintError("LL2", label, f"second run failed: {exc}"))
            if len(rows) > MAX_REFERENCE_ROWS:  # LL4
                errors.append(LintError("LL4", label, f"{len(rows)} rows > {MAX_REFERENCE_ROWS}"))
            if order_matters:  # LL3
                try:
                    probe = tie_probe_sql(sql)
                except sqlglot.errors.SqlglotError:
                    probe = None
                if probe is not None:
                    probe_sql, n, keys = probe
                    try:
                        _, prows = await run_sql(probe_sql)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(LintError("LL3", label, f"tie probe failed: {str(exc)[:200]}"))
                        continue
                    if len(prows) > n:
                        a = tuple(prows[n - 1][i] for i in keys)
                        b = tuple(prows[n][i] for i in keys)
                        if a == b:
                            errors.append(LintError("LL3", label, f"tie at LIMIT {n} cut-off: sort keys {a!r}"))
                # LL6 (extension to the spec's LL1–LL5): ties anywhere among the returned rows make row-by-row
                # comparison fragile, because a correct candidate may break the tie differently.
                try:
                    keyed = order_keys_probe_sql(sql)
                except sqlglot.errors.SqlglotError:
                    keyed = None
                if keyed is not None:
                    keyed_sql, key_idx = keyed
                    try:
                        _, krows = await run_sql(keyed_sql)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(LintError("LL6", label, f"sort-key probe failed: {str(exc)[:200]}"))
                        continue
                    for prev, cur in zip(krows, krows[1:]):
                        if tuple(prev[i] for i in key_idx) == tuple(cur[i] for i in key_idx):
                            errors.append(
                                LintError("LL6", label, f"tied sort keys inside the result: {tuple(cur[i] for i in key_idx)!r}")
                            )
                            break
    return errors


# ── CLI ──────────────────────────────────────────────────────────────────────

async def _live_main(questions: list[GoldenQuestion]) -> list[LintError]:
    from evals import pgrun  # imported lazily: needs asyncpg + fixture env

    conn = await pgrun.connect_grader()
    try:
        return await lint_live(questions, lambda sql: pgrun.run_raw(conn, sql))
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.lint")
    ap.add_argument("--live", action="store_true", help="also run LL1–LL5 against the fixture as nn_grader")
    ap.add_argument("--require-reviewed", action="store_true", help="fail on any question without reviewed_by (L7)")
    args = ap.parse_args(argv)

    errors, questions = lint_static(require_reviewed=args.require_reviewed)
    if args.live:
        from evals import guards

        guards.assert_fixture_environment()
        errors += asyncio.run(_live_main(questions))
    for err in errors:
        print(err, file=sys.stderr)
    print(f"{len(questions)} questions, {len(errors)} lint error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
