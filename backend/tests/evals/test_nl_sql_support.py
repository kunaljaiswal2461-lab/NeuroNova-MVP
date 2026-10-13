"""SPEC-08 L1-5 (cassette key, I8), L1-6 (logging, I7), L1-9 (candidate mapping) and the report additions."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest
import structlog

from agentic_engine.llm_engine.base_llm import ChatResult, LLMClient, LLMUnavailable
from agentic_engine.llm_engine.openai_client import OpenAIClient
from app.services.nl_sql import Envelope, OutputIntent, Pass1Result
from app.services.nl_sql.types import LLMCall as P1Call
from app.services.sql_execution import SqlExecutionResult, SqlResultColumn
from app.services.sql_guard import Rejection, RejectionCode
from evals import report as rpt
from evals.candidates import nl_sql as cand
from evals.candidates.base import CandidateOutcome, EvalContext
from evals.cassette import ACK_ENV, ACK_VALUE, CassetteLLM, CassetteMiss, _request, _structured_request, request_key
from evals.grader import Verdict
from evals.models import GoldenQuestion

FIXTURE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://nn:pw@app-postgres.nn-eval.test:5432/nn_app",
    ACK_ENV: ACK_VALUE,
}
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
CALL = dict(model="gpt-4o", system="SYS", user="USR", json_schema=SCHEMA, schema_name="n", max_tokens=1200, temperature=0.0, seed=7)


# ── I8: old keys never move ──────────────────────────────────────────────────

def test_chat_json_and_chat_stream_keys_are_frozen():
    json_req = _request("chat_json", model="gpt-4o", system="SYS", user="USR", history=None, max_tokens=1200, temperature=0.0)
    assert request_key(json_req) == "9ea50e15630a952fcb63124471e6dfece655018e980f4cdedf44b3e70efa46bf"
    stream_req = _request(
        "chat_stream", model="gpt-4o", system="SYS", user="USR", history=[{"role": "user", "content": "h"}],
        max_tokens=1200, temperature=0.4,
    )
    assert request_key(stream_req) == "48612834657cdc7ed3e1984bde23c0e4cdb30de1d57edbfc33868d0ca8d5ed84"


def structured_key(**changes):
    return request_key(_structured_request(**{**{k: v for k, v in CALL.items() if k != "schema_name"}, **changes}))


def test_structured_key_covers_schema_and_seed():
    base = structured_key()
    assert structured_key() == base
    assert structured_key(seed=8) != base and structured_key(seed=None) != base
    assert structured_key(json_schema={**SCHEMA, "required": ["a"]}) != base
    assert structured_key(system="SYS2") != base and structured_key(temperature=0.2) != base
    assert structured_key(json_schema={"properties": {"a": {"type": "string"}}, "type": "object"}) == base     # key order is not content


def test_structured_and_json_requests_never_collide():
    json_req = _request("chat_json", model="gpt-4o", system="SYS", user="USR", history=None, max_tokens=1200, temperature=0.0)
    assert request_key(json_req) != structured_key()


class Inner:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or ChatResult({"kind": "sql"}, "gpt-4o-2026", 11, 7)

    async def chat_structured(self, **kwargs):
        self.calls += 1
        return self.result


async def test_record_then_replay_structured(tmp_path):
    path = tmp_path / "c.jsonl"
    inner = Inner()
    rec = CassetteLLM(path, "record", inner, env=FIXTURE_ENV)
    first = await rec.chat_structured(**CALL)
    rep = CassetteLLM(path, "replay")
    again = await rep.chat_structured(**CALL)
    assert again == first and inner.calls == 1
    with pytest.raises(CassetteMiss):
        await rep.chat_structured(**{**CALL, "seed": 8})
    with pytest.raises(CassetteMiss):
        await rep.chat_structured(**{**CALL, "json_schema": {"type": "object"}})
    stored = json.loads(path.read_text().splitlines()[0])
    assert stored["request"]["seed"] == 7 and len(stored["request"]["json_schema_sha256"]) == 64
    assert "properties" not in json.dumps(stored["request"])                  # the schema itself is not stored


async def test_a_refused_response_replays_as_refused(tmp_path):
    path = tmp_path / "c.jsonl"
    refused = ChatResult({}, "gpt-4o", 5, 0, finish_reason="refusal")
    await CassetteLLM(path, "record", Inner(refused), env=FIXTURE_ENV).chat_structured(**CALL)
    replayed = await CassetteLLM(path, "replay").chat_structured(**CALL)
    assert replayed.finish_reason == "refusal" and replayed.content_json == {}


def test_cassette_and_client_satisfy_the_protocol():
    assert hasattr(LLMClient, "chat_structured")
    assert callable(CassetteLLM.chat_structured) and callable(OpenAIClient.chat_structured)


# ── I7: model content is never logged ────────────────────────────────────────

class _FakeCompletions:
    def __init__(self, message, finish="stop"):
        self.message, self.finish, self.kwargs = message, finish, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        choice = SimpleNamespace(message=self.message, finish_reason=self.finish)
        return SimpleNamespace(
            choices=[choice], model="gpt-4o-x", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3)
        )


def client_with(message, finish="stop"):
    client = OpenAIClient("sk-test")
    completions = _FakeCompletions(message, finish)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


SECRET = "SELECT secret_salary FROM internal.salaries -- zebra-9917"


async def test_chat_json_invalid_json_logs_no_content(caplog):
    client, _ = client_with(SimpleNamespace(content=SECRET, refusal=None))
    with structlog.testing.capture_logs() as events, caplog.at_level("DEBUG"):
        with pytest.raises(LLMUnavailable):
            await client.chat_json(model="m", system="s", user="u")
    assert "zebra-9917" not in json.dumps(events, default=str) + caplog.text
    (event,) = [e for e in events if e["event"] == "llm.invalid_json"]
    assert event["content_chars"] == len(SECRET) and "snippet" not in event


async def test_chat_structured_invalid_json_logs_no_content_and_does_not_raise(caplog):
    client, _ = client_with(SimpleNamespace(content=SECRET, refusal=None))
    with structlog.testing.capture_logs() as events, caplog.at_level("DEBUG"):
        result = await client.chat_structured(**CALL)
    assert "zebra-9917" not in json.dumps(events, default=str) + caplog.text
    assert result.finish_reason == "invalid_json" and result.content_json == {}


async def test_chat_structured_request_shape_and_success():
    client, completions = client_with(SimpleNamespace(content='{"kind": "sql"}', refusal=None))
    result = await client.chat_structured(**CALL)
    kw = completions.kwargs
    assert kw["response_format"] == {"type": "json_schema", "json_schema": {"name": "n", "strict": True, "schema": SCHEMA}}
    assert kw["seed"] == 7 and kw["temperature"] == 0.0 and kw["max_tokens"] == 1200
    assert kw["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}]
    assert result.content_json == {"kind": "sql"} and result.finish_reason == "stop"
    assert (result.model, result.input_tokens, result.output_tokens) == ("gpt-4o-x", 10, 3)


async def test_chat_structured_refusal_and_length():
    client, _ = client_with(SimpleNamespace(content=None, refusal="I can't help with that"))
    assert (await client.chat_structured(**CALL)).finish_reason == "refusal"
    client, _ = client_with(SimpleNamespace(content='{"kind": "s', refusal=None), finish="length")
    cut = await client.chat_structured(**CALL)
    assert cut.finish_reason == "length" and cut.content_json == {}


async def test_chat_structured_transport_error_is_llm_unavailable():
    from openai import APITimeoutError
    import httpx

    client = OpenAIClient("sk-test")

    class Boom:
        async def create(self, **kw):
            raise APITimeoutError(httpx.Request("POST", "https://x"))

    client._client = SimpleNamespace(chat=SimpleNamespace(completions=Boom()))
    with pytest.raises(LLMUnavailable):
        await client.chat_structured(**CALL)


# ── candidate mapping (B9 table) ─────────────────────────────────────────────

def question(kind="answer") -> GoldenQuestion:
    return GoldenQuestion.model_validate({
        "id": "q1", "question": "how many orders", "tags": ["aggregate"],
        "expect": {"kind": "answer", "reference_sql": "select 1", "output_type": "kpi"},
    })


def envelope_for(kind: str, sql="SELECT count(*) AS n FROM shop.orders") -> Envelope:
    intent = OutputIntent.model_validate({"type": "kpi", "encodings": {"x": None, "y": [], "series": None, "label": None, "value": "n"}})
    base = dict(sql=None, assumptions=[], tables_used=[], output_intent=None, clarification=None, reason_code=None, reason=None)
    if kind == "sql":
        base.update(sql=sql, output_intent=intent)
    elif kind == "clarification":
        base.update(clarification="Which date?")
    else:
        base.update(reason_code="not_in_schema", reason="No such data.")
    return Envelope(kind=kind, **base)


def ok_run(sql="SELECT count(*) AS n FROM shop.orders LIMIT 101", **kw) -> SqlExecutionResult:
    import uuid

    from app.services.postgres_query_executor import PostgresResultValue

    return SqlExecutionResult(
        run_id=uuid.uuid4(), status="succeeded", sql=sql, columns=(SqlResultColumn(label="n", pg_type="int8"),),
        rows=((PostgresResultValue(kind="integer", value="42"),),), row_count=1, source_id=uuid.uuid4(), **kw,
    )


def ctx(**extras) -> EvalContext:
    fixture = SimpleNamespace(
        session_factory=None, settings=SimpleNamespace(openai_api_key="k"), user_id="u", source_id="s",
        query_adapter_factory=lambda: None,
    )
    return EvalContext(
        fixture=fixture, grader_connect=None, reference_clock="2026-06-15T10:00:00+05:30",
        reference_time_zone="Asia/Kolkata", cassette_mode="replay", extras=extras,
    )


@pytest.fixture
def wired(monkeypatch):
    state = SimpleNamespace(pass1=None, executed=None, pass1_kwargs=None, exec_calls=0)

    async def fake_generate(**kwargs):
        state.pass1_kwargs = kwargs
        return state.pass1

    async def fake_execute(**kwargs):
        state.exec_calls += 1
        state.exec_kwargs = kwargs
        return state.executed

    monkeypatch.setattr(cand, "generate_sql", fake_generate)
    monkeypatch.setattr(cand, "execute_validated_sql", fake_execute)
    candidate = cand.NlSqlCandidate()
    candidate._llm = object()
    state.candidate = candidate
    return state


CALL1 = P1Call("gpt-4o", 100, 20, 12.0)


async def test_sql_that_executes_is_an_answer(wired):
    wired.pass1 = Pass1Result(envelope_for("sql"), llm_call=CALL1, rewrites=("clock_literal",))
    wired.executed = ok_run()
    out = await wired.candidate.answer(question(), ctx())
    assert out.kind == "answer" and out.result.rows == ((42,),) and out.result.complete
    assert [(c.name, c.pg_type) for c in out.result.columns] == [("n", "int8")]
    assert out.sql.endswith("LIMIT 101")                                    # the validator's text, not the model's
    assert out.output_intent["type"] == "kpi" and out.notes == ("clock_literal",)
    assert (out.llm_calls[0].model, out.llm_calls[0].input_tokens) == ("gpt-4o", 100)
    assert wired.exec_kwargs["sql"] == "SELECT count(*) AS n FROM shop.orders"          # I3: through the executor only


async def test_truncated_result_is_incomplete(wired):
    wired.pass1 = Pass1Result(envelope_for("sql"), llm_call=CALL1)
    wired.executed = ok_run(safety_row_truncated=True)
    assert (await wired.candidate.answer(question(), ctx())).result.complete is False


async def test_intent_warnings_become_notes(wired):
    wired.pass1 = Pass1Result(envelope_for("sql"), llm_call=CALL1)
    env = envelope_for("sql").model_copy(update={"output_intent": OutputIntent.model_validate(
        {"type": "pie", "encodings": {"x": None, "y": ["n", "n"], "series": None, "label": "zzz", "value": None}})})
    wired.pass1 = Pass1Result(env, llm_call=CALL1)
    wired.executed = ok_run()
    out = await wired.candidate.answer(question(), ctx())
    assert "intent_warning:unknown_label:label" in out.notes and "intent_warning:pie_multiple_y" in out.notes


async def test_clarification(wired):
    wired.pass1 = Pass1Result(envelope_for("clarification"), llm_call=CALL1)
    out = await wired.candidate.answer(question(), ctx())
    assert (out.kind, out.message) == ("clarification", "Which date?") and wired.exec_calls == 0


async def test_unsupported_is_a_refusal(wired):
    wired.pass1 = Pass1Result(envelope_for("unsupported"), llm_call=CALL1)
    out = await wired.candidate.answer(question(), ctx())
    assert (out.kind, out.message) == ("refusal", "No such data.") and wired.exec_calls == 0


async def test_validator_rejection_is_an_error_with_its_code(wired):
    wired.pass1 = Pass1Result(envelope_for("sql", sql="SELECT 1; DROP TABLE x"), llm_call=CALL1)
    wired.executed = Rejection(RejectionCode.MULTIPLE_STATEMENTS, "no")
    out = await wired.candidate.answer(question(), ctx())
    assert (out.kind, out.error_code) == ("error", "multiple_statements")
    assert out.sql == "SELECT 1; DROP TABLE x" and out.output_intent["type"] == "kpi"


async def test_execution_failure_is_an_error_with_its_code(wired):
    import uuid

    wired.pass1 = Pass1Result(envelope_for("sql"), llm_call=CALL1)
    wired.executed = SqlExecutionResult(
        run_id=uuid.uuid4(), status="failed", sql="SELECT 1", failure_code="statement_timeout", source_id=uuid.uuid4()
    )
    out = await wired.candidate.answer(question(), ctx())
    assert (out.kind, out.error_code, out.sql) == ("error", "statement_timeout", "SELECT 1")


@pytest.mark.parametrize("code", ["envelope_invalid", "llm_unavailable", "canary_leak", "over_budget", "question_too_long"])
async def test_any_pass1_failure_is_an_error(wired, code):
    wired.pass1 = Pass1Result(None, failure_code=code, llm_call=CALL1)
    out = await wired.candidate.answer(question(), ctx())
    assert (out.kind, out.error_code) == ("error", code) and wired.exec_calls == 0
    assert out.llm_calls and out.llm_calls[0].output_tokens == 20


async def test_samples_off_passes_no_sample_provider(wired):
    wired.pass1 = Pass1Result(envelope_for("clarification"), llm_call=CALL1)
    await wired.candidate.answer(question(), ctx(samples="off"))
    assert wired.pass1_kwargs["sample_values"] is None
    await wired.candidate.answer(question(), ctx())
    assert "sample_values" not in wired.pass1_kwargs                         # default = stored samples
    assert wired.pass1_kwargs["time_zone"] == "Asia/Kolkata"
    assert wired.pass1_kwargs["reference_time"].isoformat() == "2026-06-15T10:00:00+05:30"


def test_old_candidates_still_build_outcomes_without_the_new_fields():
    out = CandidateOutcome("answer")
    assert out.output_intent is None and out.notes == ()


def test_replay_needs_no_key_and_record_needs_the_ack(tmp_path, monkeypatch):
    monkeypatch.delenv(ACK_ENV, raising=False)
    monkeypatch.setattr(cand, "CASSETTE_DIR", tmp_path)
    (tmp_path / cand.CASSETTE_NAME).write_text("")
    assert isinstance(cand.NlSqlCandidate()._client(ctx()), CassetteLLM)
    record_ctx = dataclasses.replace(ctx(), cassette_mode="record")
    with pytest.raises(RuntimeError, match=ACK_ENV):
        cand.NlSqlCandidate()._client(record_ctx)


# ── report additions ─────────────────────────────────────────────────────────

def run(qid, verdict, *, expected="answer", output_type="kpi", intent=None, code=None, notes=()):
    return rpt.RunRecord(
        question_id=qid, tags=["aggregate"], output_type=output_type, expected_kind=expected, run_index=0,
        verdict=verdict, error_code=code, intent_type=intent, notes=list(notes),
    )


def test_report_aggregates_intent_codes_and_clock_rewrites():
    records = [
        run("a", Verdict.PASS, intent="kpi", notes=["clock_literal"]),
        run("b", Verdict.PASS, output_type="bar", intent="bar"),
        run("c", Verdict.PASS, output_type="bar", intent="table"),
        run("d", Verdict.FAIL_ERROR, code="column_not_found", intent="kpi"),
        run("e", Verdict.FAIL_ERROR, code="statement_timeout"),
        run("f", Verdict.PASS, expected="refusal", output_type=None),
    ]
    a = rpt.aggregate(records, pricing={})
    assert a.intent_accuracy["bar"].correct == 1 and a.intent_accuracy["bar"].runs == 2
    assert a.intent_accuracy["kpi"].runs == 3
    assert a.error_code_counts == {"column_not_found": 1, "statement_timeout": 1}
    assert a.answers_lost_to_rejection == pytest.approx(1 / 5)             # only the validator code counts; 5 answer-expected runs
    assert a.clock_rewrites == 1
    md = rpt.render_markdown(rpt.Report(
        meta=rpt.RunMeta(git_commit=None, fixture_version=1, candidate="nl_sql", suite="golden", models=[], repeat=1,
                         started_at="x", finished_at="y"),
        aggregates=a, records=records))
    assert "`column_not_found` 1" in md and "Clock rewrites" in md and "| bar | 50.0% | 2 |" in md


def test_candidates_without_an_intent_get_no_intent_table():
    a = rpt.aggregate([run("a", Verdict.PASS)], pricing={})
    assert a.intent_accuracy == {} and a.error_code_counts == {} and a.answers_lost_to_rejection is None


def test_runner_knows_the_candidate_and_the_extra_flag():
    from evals import runner

    assert isinstance(runner.load_candidate("nl_sql"), cand.NlSqlCandidate)
    assert isinstance(runner.load_candidate("evals.candidates.nl_sql:NlSqlCandidate"), cand.NlSqlCandidate)
    args = runner.build_parser().parse_args(["--candidate", "nl_sql", "--extra", "samples=off"])
    assert args.extra == ["samples=off"]
