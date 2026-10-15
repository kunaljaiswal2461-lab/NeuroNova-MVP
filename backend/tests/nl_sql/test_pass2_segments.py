"""SPEC-10 W3 / I1 (S1): value segments reproduce the resolved prose byte for byte."""
from __future__ import annotations

import random

import pytest

from app.schemas.nl_sql_record import NlSqlTurn, PresentationRecord, Segment
from app.services.nl_sql.pass2_validate import (
    MAX_RESOLVED_ANSWER_CHARS,
    Resolved,
    build_segments,
    validate_and_resolve,
)
from tests.nl_sql.pass2_helpers import D, I, T, TS, envelope, prepared, result
from tests.nl_sql.test_pass2_validate import BAR, COLS, REGION_SQL, ROWS, run, spec


def joined(segments) -> str:
    return "".join(s["text"] if s["t"] == "text" else s["exact"] for s in segments)


def resolved(raw, **kw) -> Resolved:
    out, _ex, _inp = run(raw, **kw)
    assert isinstance(out, Resolved), out
    return out


def test_every_reference_form_gets_its_kind_source_and_column():
    out = resolved(spec(
        answer="{{row:0.region}} made {{row:0.revenue}}; {{rowcount}} rows; total {{stat:revenue.sum}}; "
               "mean {{stat:revenue.mean}}; count {{stat:revenue.count}}; max {{stat:revenue.max}}.",
    ))
    values = [s for s in out.answer_segments if s["t"] == "value"]
    assert [(v["source"], v["kind"], v["column"]) for v in values] == [
        ("row", "text", "region"), ("row", "decimal", "revenue"), ("rowcount", "integer", None),
        ("stat", "decimal", "revenue"), ("stat", "decimal", "revenue"), ("stat", "integer", "revenue"),
        ("stat", "decimal", "revenue"),
    ]
    assert joined(out.answer_segments) == out.answer


def test_a_null_cell_resolves_to_a_dash_with_kind_none():
    out = resolved(spec(answer="North: {{row:0.revenue}}."), rows=[[T("North"), D(None)], [T("S"), D("2")], [T("E"), D("3")]])
    value = next(s for s in out.answer_segments if s["t"] == "value")
    assert value["exact"] == "—" and value["kind"] is None
    assert joined(out.answer_segments) == out.answer


def test_temporal_row_values_keep_their_kind():
    cols = [("month", "timestamptz"), ("revenue", "numeric")]
    rows = [[TS("2026-10-01T00:00:00Z"), D("10")], [TS("2026-11-01T00:00:00Z"), D("20")]]
    out = resolved(
        spec(display="table", encodings=dict(NO), answer="In {{row:0.month}} revenue was {{row:0.revenue}}."),
        columns=cols, rows=rows, env=envelope("table"),
    )
    kinds = [s["kind"] for s in out.answer_segments if s["t"] == "value"]
    assert kinds == ["timestamptz", "decimal"]


NO = {"x": None, "y": [], "series": None, "label": None, "value": None}


def test_takeaways_get_their_own_segments_and_s1():
    out = resolved(spec(takeaways=["{{row:1.region}} had {{row:1.revenue}}", "Only {{rowcount}} rows"]))
    assert len(out.takeaways_segments) == len(out.takeaways) == 2
    for text, segments in zip(out.takeaways, out.takeaways_segments):
        assert joined(segments) == text


def test_truncation_inside_a_value_turns_it_into_text():
    template = "x" * (MAX_RESOLVED_ANSWER_CHARS - 3) + "{{rowcount}}"           # '3' rows: cut after 3 characters? no: 1 digit
    text, segments = build_segments(template, lambda ref: ("12345", "integer", "rowcount", None), limit=MAX_RESOLVED_ANSWER_CHARS)
    assert len(text) == MAX_RESOLVED_ANSWER_CHARS and joined(segments) == text
    assert all(s["t"] == "text" for s in segments)                              # the value was cut: only text remains


def test_truncation_inside_text_keeps_whole_values_before_the_cut():
    template = "{{rowcount}} " + "y" * 50
    text, segments = build_segments(template, lambda ref: ("7", "integer", "rowcount", None), limit=10)
    assert text == "7 yyyyyyyy" and joined(segments) == text
    assert segments[0]["t"] == "value"


def test_the_fallback_path_is_one_text_segment():
    from app.services.nl_sql.pass2 import Pass2Outcome
    from app.services.nl_sql.turn_record import presentation_record, sse_presentation

    outcome = Pass2Outcome(resolved=None, fallback="llm_unavailable", input=None, llm_call=None)
    record = presentation_record(outcome, summary="Summary.", consent=True, complete=True, repaired=False)
    assert [s.model_dump(exclude_none=True) for s in record.answer_segments] == [{"t": "text", "text": "Summary."}]
    assert record.takeaways_segments == [] and record.display_time_zone == "Asia/Kolkata"
    sse = sse_presentation(record)
    assert sse["answer_segments"] == [{"t": "text", "text": "Summary."}] and sse["display_time_zone"] == "Asia/Kolkata"


def test_s1_over_200_generated_templates():
    """Segments reproduce the answer, and the answer equals the template resolved by an independent substitution."""
    rng = random.Random(1234)
    pieces = ["", " ", "a", "é", "👍🏽", "e\u0301", "x" * 37, ".", " — "]
    _out, ex, inp = run(spec(answer="{{rowcount}}"))
    expected = {
        "{{rowcount}}": "3", "{{row:0.region}}": "North", "{{row:1.revenue}}": "200",
        "{{stat:revenue.sum}}": inp.stat("revenue", "sum"), "{{stat:revenue.mean}}": inp.stat("revenue", "mean"),
    }
    assert all(expected.values())
    refs = list(expected)
    for _ in range(200):
        parts = [rng.choice(refs) if rng.random() < 0.5 else rng.choice(pieces) for _i in range(rng.randint(1, 12))]
        template = "".join(parts) + " ."
        want = " ".join(("".join(expected.get(part, part) for part in parts) + " .").split())   # sanitize folds whitespace
        out = resolved(spec(answer=template), question="q")
        assert out.answer == want, template
        assert joined(out.answer_segments) == want, template


def test_s1_negative_control_a_wrong_offset_is_detected(monkeypatch):
    """The invariant test must be able to fail: a segment builder with an off-by-one cut breaks it."""
    import app.services.nl_sql.pass2_validate as module

    real = module.build_segments

    def broken(template, resolve_ref, *, limit=None):
        text, segments = real(template, resolve_ref, limit=limit)
        if segments and segments[0]["t"] == "text":
            segments[0] = {**segments[0], "text": segments[0]["text"][1:]}        # wrong offset
        return text, segments

    monkeypatch.setattr(module, "build_segments", broken)
    out = resolved(spec(answer="North leads with {{row:0.revenue}}."))
    with pytest.raises(AssertionError):
        assert joined(out.answer_segments) == out.answer


# ── the record ───────────────────────────────────────────────────────────────

V1_ROW = {
    "version": 1, "route": "sql", "prompt_versions": {"pass1": "pass1-v2"}, "sql": "SELECT 1", "assumptions": [],
    "output_intent": None, "rewrites": [], "intent_warnings": [], "repair": None, "outcome": "answer",
    "failure_code": None,
    "presentation": {
        "display": "table", "encodings": {"x": None, "y": [], "series": None, "label": None, "value": None},
        "table_columns": [], "answer_template": "A.", "answer": "A.", "takeaways_template": [], "takeaways": [],
        "caveats": [], "fallback": None, "derived": {}, "pass2": None,
    },
    "llm": {"calls": 1, "input_tokens": 1, "output_tokens": 1},
}


def test_a_stored_v1_record_still_loads():
    record = NlSqlTurn.model_validate(V1_ROW)
    assert record.version == 1 and record.presentation.answer_segments == [] and record.presentation.display_time_zone is None


def test_a_v2_record_round_trips_and_unknown_keys_are_rejected():
    row = {**V1_ROW, "version": 2}
    row["presentation"] = {
        **V1_ROW["presentation"],
        "answer_segments": [{"t": "text", "text": "A "}, {"t": "value", "exact": "1", "kind": "integer", "source": "rowcount"}],
        "takeaways_segments": [[{"t": "text", "text": "T"}]], "display_time_zone": "Asia/Kolkata",
    }
    record = NlSqlTurn.model_validate(row)
    again = NlSqlTurn.model_validate(record.model_dump(mode="json"))
    assert again == record and isinstance(again.presentation.answer_segments[1], Segment)
    with pytest.raises(Exception):
        NlSqlTurn.model_validate({**row, "surprise": 1})
    with pytest.raises(Exception):
        PresentationRecord.model_validate({**row["presentation"], "answer_segments": [{"t": "text", "text": "x", "oops": 1}]})
