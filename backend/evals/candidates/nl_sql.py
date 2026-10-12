"""The NL->SQL candidate (SPEC-08 B9): pass 1, then ``execute_validated_sql``, mapped onto the harness outcomes.

    python -m evals.runner --suite golden --candidate evals.candidates.nl_sql:NlSqlCandidate --repeat 3 --cassette record
    python -m evals.runner --suite golden --candidate evals.candidates.nl_sql:NlSqlCandidate --extra samples=off

Replay needs neither a key nor the ACK (it never touches the network). Record and ``off`` need a key and
``STAGE4C_EVALUATION_ACK`` (INV-P0-07). Pass 1 never executes SQL: every statement reaches the database through
``execute_validated_sql`` (SPEC-08 I3).
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

from app.services.nl_sql import generate_sql, intent_warnings
from app.services.sql_execution import SqlExecutionResult, execute_validated_sql
from app.services.sql_guard import Rejection
from evals.candidates.base import CandidateOutcome, EvalContext, LLMCall
from evals.candidates.stage4c import decode_value
from evals.cassette import ACK_ENV, ACK_VALUE, CassetteLLM
from evals.grader import ResultColumn, ResultSet
from evals.models import GoldenQuestion

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "tests" / "cassettes" / "part5"
CASSETTE_NAME = "nl_sql.jsonl"


def decode_sql_result(result: SqlExecutionResult) -> ResultSet:
    columns = tuple(ResultColumn(c.label, c.pg_type) for c in result.columns)
    rows = tuple(tuple(decode_value(v.kind, v.value) for v in row) for row in result.rows)
    complete = not (result.safety_row_truncated or result.byte_truncated)
    return ResultSet(columns=columns, rows=rows, complete=complete)


class NlSqlCandidate:
    name = "nl_sql"
    requires_app = True
    uses_model = True

    def __init__(self) -> None:
        self._llm = None

    def _client(self, ctx: EvalContext):
        if self._llm is not None:
            return self._llm
        mode = ctx.cassette_mode
        path = CASSETTE_DIR / CASSETTE_NAME
        if mode == "replay":
            self._llm = CassetteLLM(path, "replay")
            return self._llm
        if os.environ.get(ACK_ENV) != ACK_VALUE:
            raise RuntimeError(f"nl_sql needs {ACK_ENV}={ACK_VALUE} to call a model (INV-P0-07)")
        from agentic_engine.llm_engine.openai_client import OpenAIClient

        real = OpenAIClient(ctx.fixture.settings.openai_api_key)
        self._llm = real if mode == "off" else CassetteLLM(path, "record", real)
        return self._llm

    async def answer(self, question: GoldenQuestion, ctx: EvalContext) -> CandidateOutcome:
        fixture = ctx.fixture
        started = time.perf_counter()
        llm = self._client(ctx)

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        # "samples=off" is the Part 4 A/B: no example values in the schema block.
        kwargs = {"sample_values": None} if ctx.extras.get("samples") == "off" else {}
        kwargs["run"] = int(ctx.extras.get("run", 0))
        p1 = await generate_sql(
            session_factory=fixture.session_factory,
            settings=fixture.settings,
            llm=llm,
            user_id=fixture.user_id,
            source_id=fixture.source_id,
            question=question.question,
            reference_time=dt.datetime.fromisoformat(ctx.reference_clock),
            time_zone=ctx.reference_time_zone,
            **kwargs,
        )
        calls: tuple[LLMCall, ...] = ()
        if p1.llm_call is not None:
            c = p1.llm_call
            calls = (LLMCall(c.model, c.input_tokens, c.output_tokens, c.latency_ms),)
        notes = tuple(p1.rewrites)

        def outcome(kind, **fields) -> CandidateOutcome:
            return CandidateOutcome(kind, latency_ms=elapsed(), llm_calls=calls, notes=notes, **fields)

        env = p1.envelope
        if env is None:
            return outcome("error", error_code=p1.failure_code)
        if env.kind == "clarification":
            return outcome("clarification", message=env.clarification)
        if env.kind == "unsupported":
            return outcome("refusal", message=env.reason)

        intent = env.output_intent.model_dump() if env.output_intent else None
        executed = await execute_validated_sql(
            session_factory=fixture.session_factory,
            settings=fixture.settings,
            user_id=fixture.user_id,
            source_id=fixture.source_id,
            sql=env.sql,
            adapter_factory=lambda _settings: fixture.query_adapter_factory(),
        )
        if isinstance(executed, Rejection):
            return outcome("error", sql=env.sql, error_code=executed.code.value, output_intent=intent)
        if not executed.is_ok:
            return outcome(
                "error", sql=executed.sql or env.sql, error_code=executed.failure_code or executed.status,
                output_intent=intent,
            )
        notes = notes + intent_warnings(env.output_intent, [(c.label, c.pg_type) for c in executed.columns])
        return outcome("answer", result=decode_sql_result(executed), sql=executed.sql, output_intent=intent)
