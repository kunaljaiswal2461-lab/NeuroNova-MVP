"""Pass 2: write the presentation spec for a result that already ran (SPEC-09 W4).

A failure here is never a failed turn (W4.5): rows and SQL are still delivered, with the templated summary and the
table. The delivery guard is checked by the caller before this runs (W4.6); this module never touches the database.
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable
from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.core.config import Settings
from app.core.logging import get_logger
from app.services.column_privacy import PrivacyFlags
from app.services.sql_execution import SqlExecutionResult

from .cancel import cancellable
from .pass2_input import Pass2Input, Pass2InputTooLarge, prepare_pass2
from .pass2_prompt import build_user2
from .pass2_spec import PASS2_JSON_SCHEMA
from .pass2_validate import Fallback, Resolved, validate_and_resolve
from .prompt import PASS2_PROMPT_VERSION, pass2_instructions
from .types import Envelope, LLMCall

logger = get_logger("nl_sql.pass2")
SCHEMA_NAME = "pass2_spec"


@dataclass(frozen=True)
class Pass2Outcome:
    resolved: Resolved | None
    fallback: str | None = None            # a hard failure: the caller shows the templated summary and the table
    llm_call: LLMCall | None = None
    input: Pass2Input | None = None
    prompt_version: str = PASS2_PROMPT_VERSION


async def write_presentation(
    *,
    llm: LLMClient,
    settings: Settings,
    question: str,
    envelope: Envelope,
    execution: SqlExecutionResult,
    flags: PrivacyFlags,
    consent: bool,
    repaired: bool,
    cancellation: PostgresExecutionCancellation | None = None,
    run: int = 0,
) -> Pass2Outcome:
    def render(candidate: Pass2Input) -> str:
        return build_user2(
            question=question, assumptions=envelope.assumptions, intent=envelope.output_intent, inp=candidate
        )

    try:
        inp = prepare_pass2(
            execution, flags=flags, consent=consent, max_rows=settings.nl_sql_pass2_max_rows,
            budget_tokens=settings.nl_sql_pass2_budget_tokens, measure=render,
        )
    except Pass2InputTooLarge:
        logger.info("nl_sql.pass2_failed", code="input_too_large")
        return Pass2Outcome(None, "input_too_large")

    extra = {"run": run} if run else {}
    started = time.perf_counter()
    try:
        result = await cancellable(
            llm.chat_structured(
                model=settings.openai_chat_model,
                system=pass2_instructions(),
                user=render(inp),
                json_schema=PASS2_JSON_SCHEMA,
                schema_name=SCHEMA_NAME,
                max_tokens=settings.nl_sql_pass2_max_output_tokens,
                temperature=0.0,
                seed=settings.nl_sql_seed,
                **extra,
            ),
            cancellation,
        )
    except LLMUnavailable:
        logger.info("nl_sql.pass2_failed", code="llm_unavailable")
        return Pass2Outcome(None, "llm_unavailable", input=inp)
    call = LLMCall(
        model=result.model, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        latency_ms=(time.perf_counter() - started) * 1000,
    )
    if result.finish_reason != "stop":
        logger.info("nl_sql.pass2_failed", code="spec_invalid")
        return Pass2Outcome(None, "spec_invalid", call, inp)
    outcome = validate_and_resolve(
        result.content_json, question=question, assumptions=envelope.assumptions, intent=envelope.output_intent,
        execution=execution, inp=inp, repaired=repaired,
    )
    if isinstance(outcome, Fallback):
        logger.info("nl_sql.pass2_failed", code=outcome.reason)
        return Pass2Outcome(None, outcome.reason, call, inp)
    return Pass2Outcome(outcome, None, call, inp)
