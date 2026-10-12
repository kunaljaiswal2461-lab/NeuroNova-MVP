"""What the answer-writing model (pass 2) is allowed to see (SPEC-09 W3.3). Pure: no I/O beyond its arguments.

* Consent off (the source default): column facts, the row count, completeness flags and the numeric statistics of
  non-personal numeric columns. No row value, and no text, temporal or personal statistic.
* Consent on: up to ``nl_sql_pass2_max_rows`` rows, every cell a fenced literal, every cell of a personal column
  replaced by a per-column placeholder (``<label>_<n>``, numbered by first appearance). The placeholder map lives in
  this object for the turn only; it is not part of ``repr`` and is never stored, logged or sent.

Statistics are computed with ``Decimal`` on the exact strings of the typed result, never through ``float``.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from app.services.column_privacy import PrivacyFlags
from app.services.schema_context.literals import untrusted_literal
from app.services.schema_context.tokenizer import O200K_COUNTER
from app.services.sql_execution import SqlExecutionResult

from .display import CATEGORY_KINDS, NUMERIC_KINDS, TEMPORAL_KINDS, canonical_decimal, kind_of
from .lineage import output_sources

CELL_LIMIT = 200
MEAN_PLACES = Decimal("0.000001")
BUDGET_CUTS: tuple[tuple[str | None, int | None], ...] = (
    (None, None), ("rows_20", 20), ("rows_5", 5), ("stats_only", 0),
)
STAT_FUNCTIONS = ("count", "nulls", "distinct", "min", "max", "sum", "mean")


class Pass2InputTooLarge(Exception):
    """Even the statistics alone do not fit the token budget."""


@dataclass(frozen=True)
class ColumnFact:
    label: str
    pg_type: str
    kind: str | None
    personal: bool
    stats: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Pass2Input:
    columns: tuple[ColumnFact, ...]
    row_count: int
    complete: bool
    safety_row_truncated: bool
    byte_truncated: bool
    consent: bool
    rows: tuple[tuple[str | None, ...], ...] = ()      # what the model sees: placeholders for personal columns
    rows_sent: int = 0
    rows_note: str | None = None
    masked_columns: tuple[str, ...] = ()
    budget_cut: str | None = None
    placeholders: frozenset[str] = field(default=frozenset(), repr=False, compare=False)

    def stat(self, label: str, function: str) -> str | None:
        for column in self.columns:
            if column.label == label:
                return column.stats.get(function)
        return None


def plain(value: Decimal) -> str:
    """A Decimal as a plain string with no exponent and no trailing zeros."""
    # ``Decimal.normalize`` rounds to the context precision (28 digits), which would silently change a big sum.
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def cell_text(cell) -> str | None:
    value = getattr(cell, "value", None)
    if getattr(cell, "kind", None) == "null" or value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


def _numeric_values(cells) -> tuple[list[Decimal], int]:
    values: list[Decimal] = []
    non_finite = 0
    for cell in cells:
        if getattr(cell, "kind", None) == "non_finite_number":
            non_finite += 1
            continue
        text = cell_text(cell)
        if text is None:
            continue
        try:
            values.append(Decimal(text))
        except InvalidOperation:
            non_finite += 1
    return values, non_finite


def _parse_temporal(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


def column_stats(cells: Sequence, kind: str | None, *, consent: bool, personal: bool) -> dict[str, str]:
    """Statistics a column may show. See the module docstring for who sees what."""
    shown_numeric = kind in NUMERIC_KINDS and not personal
    if not consent and not shown_numeric:
        return {}
    texts = [cell_text(c) for c in cells]
    present = [t for t in texts if t is not None]
    stats: dict[str, str] = {"count": str(len(texts)), "nulls": str(len(texts) - len(present))}
    if kind in NUMERIC_KINDS:
        values, non_finite = _numeric_values(cells)
        stats["distinct"] = str(len({canonical_decimal(t) or t for t in present}))
        if non_finite:
            stats["nonfinite"] = str(non_finite)
        if shown_numeric and values:
            with localcontext() as ctx:
                ctx.prec = 200
                total = sum(values, Decimal(0))
                mean = (total / Decimal(len(values))).quantize(MEAN_PLACES, rounding=ROUND_HALF_EVEN)
            stats.update(min=plain(min(values)), max=plain(max(values)), sum=plain(total), mean=plain(mean))
    else:
        stats["distinct"] = str(len(set(present)))
        if kind in TEMPORAL_KINDS and consent and not personal and present:
            parsed = [(d, t) for t in present if (d := _parse_temporal(t)) is not None]
            if parsed:
                stats["min"] = min(parsed)[1]
                stats["max"] = max(parsed)[1]
    return stats


def column_personal_flags(execution: SqlExecutionResult, flags: PrivacyFlags) -> list[bool]:
    """Per output column: personal when every source column it reads is personal; unknown counts as personal."""
    sources = output_sources(execution.sql or "", len(execution.columns))
    out: list[bool] = []
    for found in sources:
        if found is None:
            out.append(True)
        elif not found:
            out.append(False)             # reads no column (a constant, count(...)): nothing to protect
        else:
            out.append(all(flags.is_personal(s, t, c) for s, t, c in found))
    return out


def build_pass2_input(
    execution: SqlExecutionResult,
    *,
    personal: Sequence[bool],
    consent: bool,
    max_rows: int,
    cut: str | None = None,
) -> Pass2Input:
    columns = execution.columns
    rows = execution.rows
    kinds = [kind_of(c.pg_type) for c in columns]
    facts = tuple(
        ColumnFact(
            label=c.label, pg_type=c.pg_type, kind=kinds[i], personal=bool(personal[i]),
            stats=column_stats([row[i] for row in rows], kinds[i], consent=consent, personal=bool(personal[i])),
        )
        for i, c in enumerate(columns)
    )
    shown: list[tuple[str | None, ...]] = []
    masked: list[str] = []
    placeholders: set[str] = set()
    note = None
    if consent and max_rows > 0 and rows:
        chosen = rows[:max_rows]
        maps: list[dict[str, str]] = [{} for _ in columns]
        for row in chosen:
            out: list[str | None] = []
            for i, cell in enumerate(row):
                text = cell_text(cell)
                if text is not None and personal[i]:
                    pseudo = maps[i].get(text)
                    if pseudo is None:
                        pseudo = f"{columns[i].label}_{len(maps[i]) + 1}"
                        maps[i][text] = pseudo
                        placeholders.add(pseudo)
                    text = pseudo
                    if columns[i].label not in masked:
                        masked.append(columns[i].label)
                out.append(text)
            shown.append(tuple(out))
        if len(rows) > len(chosen):
            note = f"rows shown: {len(chosen)} of {len(rows)}"
    return Pass2Input(
        columns=facts,
        row_count=len(rows),
        complete=execution.status == "succeeded" and not (execution.safety_row_truncated or execution.byte_truncated),
        safety_row_truncated=execution.safety_row_truncated,
        byte_truncated=execution.byte_truncated,
        consent=consent,
        rows=tuple(shown),
        rows_sent=len(shown),
        rows_note=note,
        masked_columns=tuple(masked),
        budget_cut=cut,
        placeholders=frozenset(placeholders),
    )


def render_facts(inp: Pass2Input) -> str:
    """The result facts as prompt text. Every database-derived string is a fenced literal."""
    lines = [
        "Result facts:",
        f"- rows returned: {inp.row_count}",
        f"- complete: {'yes' if inp.complete else 'no (a safety limit was reached; totals may be incomplete)'}",
        "- columns:",
    ]
    for column in inp.columns:
        header = f"  - {untrusted_literal(column.label, limit=100)} type={column.pg_type}"
        if column.personal:
            header += " personal=true"
        lines.append(header)
        if column.stats:
            parts = [
                f"{name}={untrusted_literal(column.stats[name], limit=CELL_LIMIT)}"
                for name in (*STAT_FUNCTIONS, "nonfinite")
                if name in column.stats
            ]
            lines.append("    stats: " + " ".join(parts))
    if inp.consent and inp.rows:
        if inp.rows_note:
            lines.append(f"- {inp.rows_note}")
        lines.append("- rows (row 0 first; personal columns show placeholders):")
        for index, row in enumerate(inp.rows):
            cells = ", ".join("null" if v is None else untrusted_literal(v, limit=CELL_LIMIT) for v in row)
            lines.append(f"  row {index}: [{cells}]")
    elif not inp.consent:
        lines.append("- row values are not available for this answer")
    return "\n".join(lines)


def prepare_pass2(
    execution: SqlExecutionResult,
    *,
    flags: PrivacyFlags,
    consent: bool,
    max_rows: int,
    budget_tokens: int,
    measure: Callable[[Pass2Input], str],
    counter=O200K_COUNTER,
) -> Pass2Input:
    """Build the input and, if it is over budget, cut rows to 20, then 5, then none (statistics stay)."""
    personal = column_personal_flags(execution, flags)
    for cut, limit in BUDGET_CUTS:
        if not consent and cut is not None:
            break                                       # nothing row-shaped to cut
        inp = build_pass2_input(
            execution, personal=personal, consent=consent, cut=cut,
            max_rows=max_rows if limit is None else min(limit, max_rows),
        )
        if counter.count(measure(inp)) <= budget_tokens:
            return inp
    raise Pass2InputTooLarge()
