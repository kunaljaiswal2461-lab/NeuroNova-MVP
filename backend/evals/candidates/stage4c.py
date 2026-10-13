"""Baseline candidate: today's typed-plan pipeline (SPEC-03 §13.7).

``plan_postgres_question`` then ``execute_postgres_query``, through the fixture source and the query
adapter with the disposable test policy. Planner clarification and unsupported outcomes map to those
kinds; a typed result is decoded into a :class:`ResultSet`.

It needs a model key and ``STAGE4C_EVALUATION_ACK`` (INV-P0-07), so it is not part of CI's unit or live
jobs. Run it once with ``--repeat 3`` and commit ``baselines/stage4c.json`` to give the raw-SQL pipeline a
measured number to beat. The decoder is unit-tested; the end-to-end path needs a key and is unverified
in this repository's tests.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path

from agentic_engine.conversational.postgres_planner import plan_postgres_question
from app.services.postgres_query_executor import PostgresQueryExecutionResult, execute_postgres_query
from evals.candidates.base import CandidateOutcome, EvalContext, LLMCall
from evals.cassette import ACK_ENV, ACK_VALUE, CassetteLLM
from evals.grader import ResultColumn, ResultSet
from evals.models import GoldenQuestion

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "tests" / "cassettes" / "part0"


def decode_value(kind: str, value):
    """Typed executor value -> the Python value the grader normalises."""
    if kind == "null" or value is None:
        return None
    if kind == "boolean":
        return bool(value)
    if kind == "integer":
        return int(value)
    if kind == "decimal":
        return Decimal(value)
    if kind in ("number", "non_finite_number"):
        return float(value)
    if kind == "uuid":
        return uuid.UUID(value)
    if kind == "date":
        return dt.date.fromisoformat(value)
    if kind in ("timestamp", "timestamptz"):
        return dt.datetime.fromisoformat(value)
    return value


def decode_result(result: PostgresQueryExecutionResult) -> ResultSet:
    columns = tuple(
        ResultColumn(c.alias, c.actual_postgres_type or c.expected_postgres_type) for c in result.columns
    )
    rows = tuple(tuple(decode_value(v.kind, v.value) for v in row) for row in result.rows)
    complete = result.complete and not (result.safety_row_truncated or result.safety_byte_truncated)
    return ResultSet(columns=columns, rows=rows, complete=complete)


class Stage4cCandidate:
    name = "stage4c"
    requires_app = True

    def __init__(self) -> None:
        self._llm = None
        self._mode = None

    def _client(self, ctx: EvalContext):
        if os.environ.get(ACK_ENV) != ACK_VALUE:
            raise RuntimeError(f"stage4c needs {ACK_ENV}={ACK_VALUE} (INV-P0-07)")
        if self._llm is None:
            self._mode = ctx.cassette_mode
            if self._mode == "off":
                from agentic_engine.llm_engine.openai_client import OpenAIClient

                self._llm = OpenAIClient(ctx.fixture.settings.openai_api_key)
            elif self._mode == "record":
                from agentic_engine.llm_engine.openai_client import OpenAIClient

                self._llm = CassetteLLM(
                    CASSETTE_DIR / "stage4c.jsonl", "record", OpenAIClient(ctx.fixture.settings.openai_api_key)
                )
            else:
                self._llm = CassetteLLM(CASSETTE_DIR / "stage4c.jsonl", "replay")
        return self._llm

    async def answer(self, question: GoldenQuestion, ctx: EvalContext) -> CandidateOutcome:
        fixture = ctx.fixture
        started = time.perf_counter()
        llm = self._client(ctx)
        reference_now = dt.datetime.fromisoformat(ctx.reference_clock)

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        async with fixture.session_factory() as session:
            planned = await plan_postgres_question(
                session=session,
                owner_user_id=fixture.user_id,
                source_id=fixture.source_id,
                question=question.question,
                history=[],
                settings=fixture.settings,
                llm=llm,
                now=reference_now,
            )
        calls: tuple[LLMCall, ...] = ()
        if planned.token_usage is not None and planned.token_usage.call_count:
            calls = (
                LLMCall(
                    model=fixture.settings.openai_chat_model,
                    input_tokens=planned.token_usage.input_tokens,
                    output_tokens=planned.token_usage.output_tokens,
                    latency_ms=elapsed(),
                ),
            )
        if planned.kind == "clarification":
            return CandidateOutcome("clarification", message=planned.message, latency_ms=elapsed(), llm_calls=calls)
        if planned.kind in ("unsupported", "catalog"):
            return CandidateOutcome("refusal", message=planned.message, latency_ms=elapsed(), llm_calls=calls)
        if not planned.executable:
            return CandidateOutcome("error", error_code=f"plan_{planned.kind}", latency_ms=elapsed(), llm_calls=calls)

        executed = await execute_postgres_query(
            fixture.session_factory,
            owner_user_id=fixture.user_id,
            source_id=fixture.source_id,
            snapshot_id=planned.snapshot_id,
            plan=planned.plan,
            settings=fixture.settings,
            adapter_factory=lambda _settings: fixture.query_adapter_factory(),
        )
        if not executed.is_ok:
            return CandidateOutcome(
                "error", error_code=executed.failure_code or executed.status, latency_ms=elapsed(), llm_calls=calls
            )
        return CandidateOutcome(
            "answer", result=decode_result(executed), latency_ms=elapsed(), llm_calls=calls
        )
