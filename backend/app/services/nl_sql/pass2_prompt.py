"""The pass-2 user message (SPEC-09 W4.1): question, pass-1 assumptions and intent, result facts, display catalog."""
from __future__ import annotations

from collections.abc import Sequence

from app.services.schema_context.literals import untrusted_literal

from .pass2_input import Pass2Input, render_facts
from .types import OutputIntent

QUESTION_CHARS = 1_000
ASSUMPTION_CHARS = 200
MAX_ASSUMPTIONS = 8

DISPLAY_CATALOG = """Display catalog (what each display needs from the result):
- kpi: one row; one numeric value column (encodings.value).
- kpi_comparison: one row; two numeric columns in encodings.y as [current, previous].
- table: always possible.
- ranked_list: encodings.label (text) and encodings.value (numeric); at most 100 rows.
- bar: exactly two columns, a text label (encodings.x) then a numeric value (encodings.y[0]); unique labels; at most 100 rows.
- grouped_bar: encodings.x (text), encodings.series (text), one numeric encodings.y[0]; at most 100 x values and 12 series; each (x, series) pair once.
- line: exactly two columns, a date or timestamp (encodings.x) then a numeric value (encodings.y[0]); each time once.
- multi_line: encodings.x (date or timestamp), encodings.series (text, at most 8 values), one numeric encodings.y[0]; each (x, series) pair once.
- area: as line.
- pie: encodings.label (text) and encodings.value (numeric, not negative); at most 6 rows.
- scatter: two numeric columns, encodings.x and encodings.y[0]; at most 500 rows.
- histogram: encodings.x (text or numeric bin label) and a numeric count in encodings.y[0]; at most 100 rows.
- heatmap: encodings.x (text), encodings.series (text), encodings.value (numeric); at most 50 by 50.
- pivot: encodings.x, encodings.series (text, at most 30 values), encodings.value (numeric); each (x, series) pair once.
- narrative: the answer text only.
- takeaways: the list of takeaways only."""


def _intent_line(intent: OutputIntent | None) -> str:
    if intent is None:
        return "Requested output: none stated"
    e = intent.encodings
    parts = [f"type={intent.type}"]
    for name, value in (("x", e.x), ("series", e.series), ("label", e.label), ("value", e.value)):
        if value is not None:
            parts.append(f"{name}={untrusted_literal(value, limit=100)}")
    if e.y:
        parts.append("y=[" + ", ".join(untrusted_literal(y, limit=100) for y in e.y) + "]")
    return "Requested output: " + " ".join(parts)


def build_user2(
    *, question: str, assumptions: Sequence[str], intent: OutputIntent | None, inp: Pass2Input
) -> str:
    items = ", ".join(untrusted_literal(a, limit=ASSUMPTION_CHARS) for a in list(assumptions)[:MAX_ASSUMPTIONS])
    return "\n\n".join([
        f"Question: {untrusted_literal(question, limit=QUESTION_CHARS)}",
        f"Assumptions made when writing the query: [{items}]",
        _intent_line(intent),
        render_facts(inp),
        DISPLAY_CATALOG,
    ])
