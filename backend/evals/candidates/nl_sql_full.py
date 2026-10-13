"""The full NL->SQL pipeline as an eval candidate (SPEC-09 W7): pass 1, at most one repair, execution, then pass 2.

    python -m evals.runner --suite golden --candidate evals.candidates.nl_sql_full:NlSqlFullCandidate --repeat 3 --cassette record
    python -m evals.runner --suite chains --candidate nl_sql_full --extra repair=off
    python -m evals.runner --suite golden --candidate nl_sql_full --extra cassette=part9_stub      # CI: scripted stub, no key

Variants (``--extra key=value``): ``samples=off`` (no example values in the schema block), ``consent=on|off`` (rows to
pass 2; default on, the setting the tester records with), ``repair=off`` (the first attempt only: reproduces the Part 5
candidate's numbers on the same cassette family), ``cassette=<dir under tests/cassettes>`` (default ``part9``).

Replay needs neither a key nor the ACK. Record and ``off`` need a key and ``STAGE4C_EVALUATION_ACK`` (INV-P0-07).
Every statement reaches the database through ``execute_validated_sql`` (the pipeline's only door, I3).
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.schemas.nl_sql_record import NlSqlTurn
from app.services import column_privacy
from app.services.nl_sql import answer_question
from app.services.nl_sql.history import history_from_messages
from app.services.nl_sql.pass2 import write_presentation
from app.services.nl_sql.pipeline import AttemptRecord, PipelineOutcome
from app.services.nl_sql.turn_record import presentation_record
from app.services.nl_sql.types import Turn
from evals.candidates.base import CandidateOutcome, EvalContext, LLMCall
from evals.candidates.nl_sql import decode_sql_result
from evals.cassette import ACK_ENV, ACK_VALUE, CassetteLLM
from evals.models import GoldenQuestion

CASSETTES_DIR = Path(__file__).resolve().parents[2] / "tests" / "cassettes"
DEFAULT_CASSETTE_DIR = "part9"
CASSETTE_NAME = "nl_sql_full.jsonl"
PERSONAL_SQL = "SELECT full_name, email, phone FROM shop.customers"
PERSONAL_LEAK_NOTE = "pass2_personal_value_in_prompt"


def chain_history(turns: tuple[dict, ...] | None) -> list[Turn]:
    """The earlier exchanges of a chain, through the SAME history builder the chat uses (W6.6).

    An answered turn contributes its question and its *reference* SQL; a clarification turn the question and the
    assistant's question; a failed turn the question only. The final turn is never part of it.
    """
    messages: list[Any] = []
    for turn in turns or ():
        messages.append(SimpleNamespace(role="USER", content=turn["question"]))
        if "clarification" in turn:
            record = NlSqlTurn(outcome="clarification")
            messages.append(SimpleNamespace(role="ASSISTANT", content=turn["clarification"], nl_sql=record))
        elif turn.get("failed"):
            messages.append(SimpleNamespace(role="ASSISTANT", content="", nl_sql=NlSqlTurn(outcome="failed")))
        else:
            record = NlSqlTurn(outcome="answer", sql=turn["reference_sql"].strip())
            messages.append(SimpleNamespace(role="ASSISTANT", content="", nl_sql=record))
    return history_from_messages(messages)


def _calls(outcome_calls) -> tuple[LLMCall, ...]:
    return tuple(LLMCall(c.model, c.input_tokens, c.output_tokens, c.latency_ms) for c in outcome_calls)


def _from_attempt(attempt: AttemptRecord, *, message: str | None = None) -> CandidateOutcome:
    """What the turn would have been had it stopped after this attempt (the 'without repair' view)."""
    if attempt.outcome == "answer" and attempt.execution is not None:
        return CandidateOutcome("answer", result=decode_sql_result(attempt.execution), sql=attempt.sql)
    if attempt.outcome == "clarification":
        return CandidateOutcome("clarification", message=message)
    if attempt.outcome == "unsupported":
        return CandidateOutcome("refusal", message=message)
    return CandidateOutcome("error", sql=attempt.sql, error_code=attempt.code)


class _Recorder:
    """Passes calls to the real client and keeps each pass-2 user message for the personal-value scan."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.pass2_prompts: list[str] = []

    async def chat_structured(self, **kwargs):
        if kwargs.get("schema_name") == "pass2_spec":
            self.pass2_prompts.append(kwargs["user"])
        return await self._inner.chat_structured(**kwargs)


class NlSqlFullCandidate:
    name = "nl_sql_full"
    requires_app = True
    uses_model = True

    def __init__(self) -> None:
        self._llm = None
        self._personal: tuple[str, ...] | None = None

    def _client(self, ctx: EvalContext):
        if self._llm is not None:
            return self._llm
        path = CASSETTES_DIR / ctx.extras.get("cassette", DEFAULT_CASSETTE_DIR) / CASSETTE_NAME
        mode = ctx.cassette_mode
        if mode == "replay":
            self._llm = CassetteLLM(path, "replay")
            return self._llm
        if os.environ.get(ACK_ENV) != ACK_VALUE:
            raise RuntimeError(f"nl_sql_full needs {ACK_ENV}={ACK_VALUE} to call a model (INV-P0-07)")
        if ctx.extras.get("scripted"):
            # ``--extra scripted=<jsonl>``: the scripted stub stands in for the model (how the stub cassettes are made).
            from agentic_engine.llm_engine.scripted_client import ScriptedLLM

            real = ScriptedLLM(ctx.extras["scripted"])
        else:
            from agentic_engine.llm_engine.openai_client import OpenAIClient

            real = OpenAIClient(ctx.fixture.settings.openai_api_key)
        self._llm = real if mode == "off" else CassetteLLM(path, "record", real)
        return self._llm

    async def _personal_values(self, ctx: EvalContext) -> tuple[str, ...]:
        if self._personal is None:
            conn = await ctx.grader_connect()
            try:
                rows = await conn.fetch(PERSONAL_SQL)
            finally:
                await conn.close()
            self._personal = tuple(sorted({v for row in rows for v in row if isinstance(v, str) and len(v) >= 6}))
        return self._personal

    async def answer(self, question: GoldenQuestion, ctx: EvalContext) -> CandidateOutcome:
        fixture = ctx.fixture
        started = time.perf_counter()
        recorder = _Recorder(self._client(ctx))
        extras = ctx.extras
        run = int(extras.get("run", 0))
        consent = extras.get("consent", "on") != "off"
        sample_kwargs = {"sample_values": None} if extras.get("samples") == "off" else {}

        pipeline: PipelineOutcome = await answer_question(
            session_factory=fixture.session_factory,
            settings=fixture.settings,
            llm=recorder,
            user_id=fixture.user_id,
            source_id=fixture.source_id,
            question=question.question,
            history=chain_history(question.turns),
            reference_time=dt.datetime.fromisoformat(ctx.reference_clock),
            time_zone=ctx.reference_time_zone,
            execute_kwargs={"adapter_factory": lambda _settings: fixture.query_adapter_factory()},
            run=run,
            allow_repair=extras.get("repair") != "off",
            **sample_kwargs,
        )
        calls = _calls(pipeline.llm_calls)
        notes = tuple(pipeline.rewrites)
        first_message = None
        first = _from_attempt(pipeline.attempts[0], message=first_message) if pipeline.attempts else None
        repair_reason = pipeline.repair.reason_code if pipeline.repair else None
        # A repair that ended in a failure still counts as attempted: read it from the attempts.
        if repair_reason is None and len(pipeline.attempts) > 1 and pipeline.attempts[0].outcome in ("rejected", "failed", "pass1_failed"):
            repair_reason = pipeline.attempts[0].code
        intent = pipeline.envelope.output_intent.model_dump() if pipeline.envelope and pipeline.envelope.output_intent else None

        def outcome(kind: str, **fields: Any) -> CandidateOutcome:
            return CandidateOutcome(
                kind, latency_ms=(time.perf_counter() - started) * 1000, llm_calls=calls, notes=notes,
                output_intent=intent, repair_reason=repair_reason, first_attempt=first, **fields,
            )

        if pipeline.kind == "clarification":
            return outcome("clarification", message=pipeline.user_message)
        if pipeline.kind == "unsupported":
            return outcome("refusal", message=pipeline.user_message)
        if pipeline.kind == "failed":
            return outcome("error", sql=pipeline.sql, error_code=pipeline.failure_code)

        execution = pipeline.execution
        assert execution is not None
        notes = notes + pipeline.intent_warnings
        # ── pass 2 ────────────────────────────────────────────────────────────────────────────────
        async with fixture.session_factory() as session:
            try:
                flags = await column_privacy.load_effective_flags(session, fixture.source_id)
            finally:
                await session.rollback()
        pass2 = await write_presentation(
            llm=recorder, settings=fixture.settings, question=question.question, envelope=pipeline.envelope,
            execution=execution, flags=flags, consent=consent, repaired=pipeline.repair is not None, run=run,
        )
        if pass2.llm_call is not None:
            c = pass2.llm_call
            calls = calls + (LLMCall(c.model, c.input_tokens, c.output_tokens, c.latency_ms),)
        complete = not (execution.safety_row_truncated or execution.byte_truncated)
        presentation = presentation_record(
            pass2, summary="", consent=consent, complete=complete, repaired=pipeline.repair is not None
        )
        if consent and recorder.pass2_prompts:
            # A name the user typed in the question is the user's own text, not a leak from the rows.
            personal = tuple(v for v in await self._personal_values(ctx) if v not in question.question)
            if any(value in prompt for prompt in recorder.pass2_prompts for value in personal):
                notes = notes + (PERSONAL_LEAK_NOTE,)
        return outcome(
            "answer",
            result=decode_sql_result(execution),
            sql=execution.sql,
            presentation=presentation.model_dump(mode="json"),
            pass2_fallback=pass2.fallback or (
                presentation.fallback if presentation.fallback else None
            ),
            pass2_ran=True,
            pass2_dropped=pass2.resolved.dropped if pass2.resolved is not None else 0,
        )
