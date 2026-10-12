"""Pass 1: question + schema block -> one strict envelope (SPEC-08 B1, B2).

Does not execute anything. Exactly one model call, no retry (repair is Part 6). Logs codes and sizes only: never the
question, the SQL or any model output.
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable
from app.core.config import Settings
from app.core.logging import get_logger
from app.exceptions.custom_exceptions import AppError
from app.services import catalog_service
from app.services.schema_context import STORED_SAMPLES, SchemaContextError, build_schema_context

from . import messages
from .clock import rewrite_clock
from .envelope import ENVELOPE_JSON_SCHEMA, EnvelopeError, parse_envelope
from .prompt import PROMPT_VERSION, build_system, build_user
from .write_guard import is_write_or_ddl_request
from .types import Envelope, LLMCall, Pass1Result, Turn

logger = get_logger("nl_sql.pass1")

SCHEMA_NAME = "pass1_envelope"
WRITE_REFUSAL = messages.WRITE_REFUSAL


def _failed(code: str, **fields: Any) -> Pass1Result:
    logger.info("nl_sql.pass1_failed", code=code)
    return Pass1Result(envelope=None, failure_code=code, prompt_version=PROMPT_VERSION, **fields)


async def generate_sql(
    *,
    session_factory,
    settings: Settings,
    llm: LLMClient,
    user_id: UUID,
    source_id: UUID,
    question: str,
    history: Sequence[Turn] = (),
    reference_time: datetime,
    time_zone: str,
    sample_values=STORED_SAMPLES,
    run: int = 0,
    repair_brief: str | None = None,
) -> Pass1Result:
    # 1. Input checks. No model call for any of them.
    question = question.replace("\x00", "")
    if not question.strip():
        return _failed("empty_question")
    if len(question) > settings.nl_sql_max_question_chars:
        return _failed("question_too_long")

    # 2. Write guard (SPEC-08 F6): a mutation request never reaches the model.
    if is_write_or_ddl_request(question):
        logger.info("nl_sql.pass1_write_guard")
        return Pass1Result(
            envelope=Envelope(
                kind="unsupported", sql=None, assumptions=[], tables_used=[], output_intent=None,
                clarification=None, reason_code="write_request", reason=WRITE_REFUSAL,
            ),
            prompt_version=PROMPT_VERSION,
        )

    # 3. Schema of the current head snapshot.
    try:
        async with session_factory() as session:
            try:
                context = await catalog_service.get_postgres_chat_catalog_context_for_owner(
                    session, source_id=source_id, owner_user_id=user_id
                )
                snapshot_id = context.snapshot.id
                schema = await build_schema_context(
                    session, owner_user_id=user_id, source_id=source_id, snapshot_id=snapshot_id,
                    settings=settings, sample_values=sample_values,
                )
            finally:
                await session.rollback()      # a read-only turn: never leave a transaction open across the model call
    except SchemaContextError as exc:
        return _failed(exc.code)
    except catalog_service.CatalogServiceError:
        return _failed("catalog_unavailable")
    except AppError:
        return _failed("source_unavailable")
    except SQLAlchemyError:
        return _failed("catalog_unavailable")
    known = dict(snapshot_id=snapshot_id, schema_fingerprint=schema.fingerprint)

    # 4. One model call.
    system = build_system(schema.text)
    user = build_user(
        question=question, reference_time=reference_time, time_zone=time_zone, history=history,
        max_question_chars=settings.nl_sql_max_question_chars, repair_brief=repair_brief,
    )
    started = time.perf_counter()
    extra = {"run": run} if run else {}      # cassette sample index only (SPEC-09 W0.2)
    try:
        result = await llm.chat_structured(
            model=settings.openai_chat_model,
            system=system,
            user=user,
            json_schema=ENVELOPE_JSON_SCHEMA,
            schema_name=SCHEMA_NAME,
            max_tokens=settings.nl_sql_max_output_tokens,
            temperature=0.0,
            seed=settings.nl_sql_seed,
            **extra,
        )
    except LLMUnavailable:
        return _failed("llm_unavailable", **known)
    call = LLMCall(
        model=result.model, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        latency_ms=(time.perf_counter() - started) * 1000,
    )

    # 5. Parse. A refusal or a cut-off answer is an invalid envelope; there is no retry here.
    if result.finish_reason != "stop":
        detail = {"length": "length", "refusal": "refusal", "invalid_json": "invalid_json"}.get(
            result.finish_reason, "not_valid"
        )
        return _failed("envelope_invalid", llm_call=call, failure_detail=detail, **known)
    try:
        envelope, rewrites = parse_envelope(result.content_json)
    except EnvelopeError as exc:
        return _failed(exc.code, llm_call=call, failure_detail="not_valid" if exc.code == "envelope_invalid" else None, **known)

    # 6. Clock rewrite, SQL only.
    if envelope.kind == "sql":
        new_sql, changed = rewrite_clock(envelope.sql, reference_time, time_zone)
        if changed:
            envelope = envelope.model_copy(update={"sql": new_sql})
            rewrites = (*rewrites, "clock_literal")

    return Pass1Result(
        envelope=envelope, prompt_version=PROMPT_VERSION, llm_call=call, rewrites=tuple(rewrites), **known
    )
