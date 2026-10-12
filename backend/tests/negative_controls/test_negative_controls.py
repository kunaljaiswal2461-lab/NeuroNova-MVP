"""SPEC-09 L1-16: every privacy or leak test must first be SEEN FAILING with its protection deliberately disabled.

Each test below switches one protection off with ``monkeypatch`` and then runs the same check the real test makes, inside
``pytest.raises(AssertionError)``. If the check still passed with the protection off, the real test would prove nothing.
"""
from __future__ import annotations

import pytest

from app.services.nl_sql import pass2_input, pass2_validate, repair
from app.services.nl_sql.pass2_prompt import build_user2
from app.services.nl_sql.pass2_validate import Fallback, Resolved, validate_and_resolve
from app.services.sql_guard import RejectionCode
from tests.nl_sql.pass2_helpers import D, I, T, envelope, flags, prepared, result
from tests.nl_sql.helpers import ScriptedExecutor, failed_result, rejection, sql_env, ok_result
from tests.nl_sql.conftest import StubLLM, synced  # noqa: F401  (fixtures)
from tests.sample_values.conftest import factory, settings, world  # noqa: F401

SQL = "SELECT c.full_name AS full_name, c.email AS email, sum(o.amount) AS revenue FROM sales.customers AS c JOIN sales.orders AS o ON true GROUP BY 1, 2"
PERSONAL = {("sales", "customers", "full_name"), ("sales", "customers", "email")}
COLS = [("full_name", "text"), ("email", "text"), ("revenue", "numeric")]


def customers(n=6):
    return result(COLS, [[T(f"Person Number {i}"), T(f"person{i}@example.com"), D(f"{i}.50")] for i in range(n)], sql=SQL)


def check_no_text_or_personal_value(prompt, ex):
    for row in ex.rows:
        for cell in row[:2]:
            assert cell.value not in prompt, f"leaked {cell.value!r}"


# ── I4 / I5 ───────────────────────────────────────────────────────────────────

def test_i4_consent_off_check_fails_when_rows_are_sent_anyway(monkeypatch):
    ex = customers()
    real = pass2_input.build_pass2_input

    def leaky(execution, *, personal, consent, max_rows, cut=None):
        # protection off: rows are sent although consent is off, and nothing is masked
        return real(execution, personal=[False] * len(personal), consent=True, max_rows=max_rows, cut=cut)

    monkeypatch.setattr(pass2_input, "build_pass2_input", leaky)
    inp = prepare_with(ex, consent=False)
    prompt = build_user2(question="q", assumptions=[], intent=None, inp=inp)
    with pytest.raises(AssertionError, match="leaked"):
        check_no_text_or_personal_value(prompt, ex)


def test_i5_masking_check_fails_when_the_personal_flags_are_ignored(monkeypatch):
    ex = customers()
    monkeypatch.setattr(pass2_input, "column_personal_flags", lambda execution, privacy: [False] * len(execution.columns))
    inp = prepare_with(ex, consent=True)
    prompt = build_user2(question="q", assumptions=[], intent=None, inp=inp)
    with pytest.raises(AssertionError, match="leaked"):
        check_no_text_or_personal_value(prompt, ex)


def prepare_with(ex, *, consent):
    return pass2_input.prepare_pass2(
        ex, flags=flags(PERSONAL), consent=consent, max_rows=50, budget_tokens=6000,
        measure=lambda inp: build_user2(question="q", assumptions=[], intent=None, inp=inp),
    )


def test_the_unmodified_code_passes_both_checks():
    ex = customers()
    for consent in (False, True):
        prompt = build_user2(question="q", assumptions=[], intent=None, inp=prepare_with(ex, consent=consent))
        check_no_text_or_personal_value(prompt, ex)


# ── I6: the number check ──────────────────────────────────────────────────────

ROWS = [[T("North"), D("100.50")], [T("South"), D("200")]]
REGION_SQL = "SELECT o.region AS region, sum(o.amount) AS revenue FROM sales.orders AS o GROUP BY 1"


def validated(answer):
    ex = result([("region", "text"), ("revenue", "numeric")], ROWS, sql=REGION_SQL)
    env = envelope("table")
    inp = prepared(ex, consent=True, env=env)
    raw = {"display": "table", "encodings": {"x": None, "y": [], "series": None, "label": None, "value": None},
           "table_columns": [], "answer": answer, "takeaways": [], "caveats": []}
    return validate_and_resolve(raw, question="q", assumptions=[], intent=env.output_intent, execution=ex, inp=inp, repaired=False)


def test_i6_invented_figures_are_dropped_and_would_pass_with_the_check_off(monkeypatch):
    out = validated("Fine. Revenue was 7777 last year.")
    assert isinstance(out, Resolved) and "7777" not in out.answer                 # protection on
    monkeypatch.setattr(pass2_validate, "figure_is_allowed", lambda identity, allowed: True)
    out_off = validated("Fine. Revenue was 7777 last year.")
    assert isinstance(out_off, Resolved) and "7777" in out_off.answer             # the invented figure now gets through
    with pytest.raises(AssertionError):
        assert "7777" not in out_off.answer                                        # so the real assertion fails


# ── I7: markup in pass-2 text ─────────────────────────────────────────────────

def test_i7_markup_would_reach_the_screen_with_sanitising_off(monkeypatch):
    out = validated("<img src=x onerror=alert(1)>Hello.")
    assert isinstance(out, Resolved) and "<" not in out.answer
    monkeypatch.setattr(pass2_validate, "sanitize", lambda text: text)
    out_off = validated("<img src=x onerror=alert(1)>Hello.")
    assert isinstance(out_off, Resolved)
    with pytest.raises(AssertionError):
        assert "<" not in out_off.answer


# ── I11: the repair brief ─────────────────────────────────────────────────────

def test_i11_server_text_would_enter_the_brief_if_the_brief_used_it():
    server_text = "DETAIL: Key (email)=(x@y.z) already exists. HINT: use another"
    result_ = failed_result("query_error", error_class="42703", message=server_text)
    classification = repair.classify_execution(result_)
    brief = repair.render_brief(classification, "SELECT 1")
    assert "DETAIL" not in brief and "x@y.z" not in brief                         # protection on
    leaky = repair.Classification(classification.outcome, classification.code, classification.reason_code, server_text)
    with pytest.raises(AssertionError):
        assert "DETAIL" not in repair.render_brief(leaky, "SELECT 1")             # a brief built from server text is caught


# ── I13: CSV injection (frontend) is negative-controlled in tests/answers.test.jsx ────────────────────────

# ── I1: the call budget ───────────────────────────────────────────────────────

async def test_i1_the_counted_budget_is_what_allows_the_one_repair(monkeypatch, synced):
    """The pipeline has no loop, so a third call cannot be made by lifting a number; what the counter controls is the
    one repair itself. With the budget at 0 there is no repair call; at 1 there is exactly one; the L1 sequences assert
    that no path ever shows a third."""
    from app.services.nl_sql import answer_question, pipeline
    from tests.nl_sql.conftest import REF_TIME, ZONE

    kwargs = dict(session_factory=synced.factory, settings=synced.settings, user_id=synced.owner_id,
                  source_id=synced.source_id, question="how many", reference_time=REF_TIME, time_zone=ZONE)

    async def calls_with(budget):
        monkeypatch.setattr(pipeline, "MAX_EXTRA_CALLS", budget)
        llm = StubLLM([sql_env("SELECT a FROM sales.orders"), sql_env("SELECT b FROM sales.orders"), sql_env("SELECT c FROM sales.orders")])
        script = [rejection(RejectionCode.COLUMN_NOT_FOUND, "a"), rejection(RejectionCode.COLUMN_NOT_FOUND, "b"), rejection(RejectionCode.COLUMN_NOT_FOUND, "c")]
        await answer_question(llm=llm, execute=ScriptedExecutor(script), **kwargs)
        return len(llm.calls)

    assert await calls_with(0) == 1
    assert await calls_with(1) == 2
    assert await calls_with(5) == 2                                                # no loop: more budget never means a third call
