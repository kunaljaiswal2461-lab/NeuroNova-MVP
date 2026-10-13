"""Writes the scripted stub cassettes for CI (SPEC-09 W7.4): the full pipeline replays end to end with no key.

Pass 1 answers with each golden question's REFERENCE SQL; pass 2 answers with a display taken from the question's
``output_type``. A fixed rotation makes some questions take the repair path, an invalid pass-1 envelope, a failed repair,
or one of the pass-2 fallbacks, so every state-machine sequence appears in the cassette.

    docker compose -f backend/evals/fixture/compose.yml run --rm runner python -m evals.stub_cassette

writes ``tests/cassettes/part9_stub/nl_sql_full.jsonl`` and ``tests/cassettes/part9_stub/script.jsonl``. The cassette is
keyed by the exact requests (schema text included), so it must be regenerated whenever a prompt or the fixture changes.
Replay: ``python -m evals.runner --suite golden --candidate nl_sql_full --cassette replay --repeat 1 --extra cassette=part9_stub --extra consent=off``.
Consent is off because a query without ORDER BY returns rows in a different order on each run, and with consent on the rows are in the pass-2 prompt (a different request key).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from evals import pgrun
from evals.cassette import ACK_ENV, ACK_VALUE
from evals.models import GoldenQuestion, load_questions

OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "cassettes" / "part9_stub"
SCRIPT_NAME = "script.jsonl"
NO = {"x": None, "y": [], "series": None, "label": None, "value": None}

# (display) -> how to name the columns of the result. Falls back to a table when the result is too narrow.
_ENCODINGS = {
    "kpi": lambda c: {**NO, "value": c[0]},
    "kpi_comparison": lambda c: {**NO, "y": [c[0], c[1]]} if len(c) >= 2 else None,
    "ranked_list": lambda c: {**NO, "label": c[0], "value": c[1]} if len(c) >= 2 else None,
    "pie": lambda c: {**NO, "label": c[0], "value": c[1]} if len(c) >= 2 else None,
    "bar": lambda c: {**NO, "x": c[0], "y": [c[1]]} if len(c) >= 2 else None,
    "line": lambda c: {**NO, "x": c[0], "y": [c[1]]} if len(c) >= 2 else None,
    "area": lambda c: {**NO, "x": c[0], "y": [c[1]]} if len(c) >= 2 else None,
    "histogram": lambda c: {**NO, "x": c[0], "y": [c[1]]} if len(c) >= 2 else None,
    "scatter": lambda c: {**NO, "x": c[0], "y": [c[1]]} if len(c) >= 2 else None,
    "grouped_bar": lambda c: {**NO, "x": c[0], "series": c[1], "y": [c[2]]} if len(c) >= 3 else None,
    "multi_line": lambda c: {**NO, "x": c[0], "series": c[1], "y": [c[2]]} if len(c) >= 3 else None,
    "heatmap": lambda c: {**NO, "x": c[0], "series": c[1], "value": c[2]} if len(c) >= 3 else None,
    "pivot": lambda c: {**NO, "x": c[0], "series": c[1], "value": c[2]} if len(c) >= 3 else None,
}


def display_and_encodings(output_type: str | None, columns: list[str]) -> tuple[str, dict]:
    build = _ENCODINGS.get(output_type or "")
    encodings = build(columns) if build else None
    if encodings is None:
        return ("narrative" if output_type == "narrative" else "table"), dict(NO)
    return output_type, encodings                                   # type: ignore[return-value]


def envelope(sql: str, display: str, encodings: dict) -> dict:
    return {
        "kind": "sql", "sql": sql, "assumptions": ["scripted stub: the golden reference query"], "tables_used": [],
        "output_intent": {"type": display, "encodings": encodings},
        "clarification": None, "reason_code": None, "reason": None,
    }


def spec(display: str, encodings: dict, answer: str = "The result has {{rowcount}} rows.") -> dict:
    return {"display": display, "encodings": encodings, "table_columns": [], "answer": answer, "takeaways": [], "caveats": []}


def behaviour(index: int, q: GoldenQuestion) -> str:
    if q.expect.kind != "answer":
        return "normal"
    return {1: "repair_column", 2: "repair_envelope", 3: "repair_fails", 4: "pass2_invalid", 5: "pass2_number", 6: "pass2_unavailable"}.get(index % 12, "normal")


def lines_for(index: int, q: GoldenQuestion, columns: list[str]) -> list[dict]:
    match = q.question
    p1 = lambda response, **extra: {"schema": "pass1_envelope", "match": match, "exact": True, "response": response, **extra}
    p2 = lambda response, **extra: {"schema": "pass2_spec", "match": match, "exact": True, "response": response, **extra}
    if q.expect.kind == "clarification":
        return [p1({"kind": "clarification", "sql": None, "assumptions": [], "tables_used": [], "output_intent": None,
                    "clarification": "Could you say which one you mean?", "reason_code": None, "reason": None})]
    if q.expect.kind == "refusal":
        code = "write_request" if q.expect.category == "write" else "out_of_scope"
        return [p1({"kind": "unsupported", "sql": None, "assumptions": [], "tables_used": [], "output_intent": None,
                    "clarification": None, "reason_code": code, "reason": "Not something this source can answer."})]
    sql = q.expect.reference_sql.strip()
    display, encodings = display_and_encodings(q.expect.output_type, columns)
    good = envelope(sql, display, encodings)
    mode = behaviour(index, q)
    out: list[dict] = []
    if mode == "repair_column":
        out += [p1(envelope("SELECT no_such_column_zz FROM shop.orders", display, encodings), brief=False), p1(good, brief=True)]
    elif mode == "repair_envelope":
        out += [p1({"kind": "sql"}, brief=False), p1(good, brief=True)]
    elif mode == "repair_fails":
        out += [p1(envelope("SELECT no_such_column_zz FROM shop.orders", display, encodings))]       # the repair repeats it
    else:
        out += [p1(good)]
    if mode == "repair_fails":
        return out
    if mode == "pass2_invalid":
        out += [p2({"display": "kpi"})]
    elif mode == "pass2_number":
        out += [p2(spec(display, encodings, "The total is 987654321 in all."))]
    elif mode == "pass2_unavailable":
        # A cassette stores responses, not exceptions, so "the model was unavailable" is recorded as a refusal.
        out += [p2({}, finish_reason="refusal")]
    else:
        out += [p2(spec(display, encodings))]
    return out


async def build_script(questions: list[GoldenQuestion]) -> list[dict]:
    conn = await pgrun.connect_grader()
    lines: list[dict] = []
    try:
        for index, q in enumerate(questions):
            columns: list[str] = []
            if q.expect.reference_sql:
                columns = [c.name for c in (await pgrun.run_result(conn, q.expect.reference_sql, max_rows=5)).columns]
            lines.extend(lines_for(index, q, columns))
    finally:
        await conn.close()
    return lines


def main(argv: list[str] | None = None) -> int:
    os.environ[ACK_ENV] = ACK_VALUE      # the compose file passes an empty string through
    from evals import runner

    questions = load_questions("golden") + load_questions("adversarial") + load_questions("chains")
    lines = asyncio.run(build_script(questions))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    script = OUT_DIR / SCRIPT_NAME
    script.write_text("\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n", encoding="utf-8")
    cassette = OUT_DIR / "nl_sql_full.jsonl"
    if cassette.exists():
        cassette.unlink()
    code = 0
    for suite in ("golden", "adversarial", "chains"):
        code |= runner.main([
            "--suite", suite, "--candidate", "nl_sql_full", "--cassette", "record", "--repeat", "1",
            "--concurrency", "1", "--extra", "consent=off", "--extra", "cassette=part9_stub", "--extra", f"scripted={script}",
        ])
    print(f"{len(lines)} scripted answers -> {script}; cassette -> {cassette}")
    return code


if __name__ == "__main__":
    sys.exit(main())
