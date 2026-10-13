"""The NL->SQL turn: pass 1, execution, at most one repair, execution again (SPEC-09 W1).

``answer_question`` is the single entry point used by the chat agent and by the eval candidate. Nothing here talks
to the database except through ``execute_validated_sql`` (I3), and the whole turn makes at most two pass-1 model
calls: the first, and one extra for a repair *or* a regenerate (I1). The extra call is counted in code.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import sqlglot
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from agentic_engine.llm_engine.base_llm import LLMClient
from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.core.config import Settings
from app.core.logging import get_logger
from app.services import catalog_service
from app.services.schema_context import STORED_SAMPLES
from app.services.sql_execution import SqlExecutionResult, execute_validated_sql
from app.services.sql_guard import Rejection, load_catalog_scope

from . import messages
from .cancel import TurnCancelled as _TurnCancelled
from .cancel import cancellable as _cancellable
from .generate import generate_sql
from .pass2_validate import sanitize
from .intent import intent_warnings
from .repair import Classification, Outcome, classify_execution, classify_pass1_failure, classify_rejection, render_brief
from .types import Envelope, LLMCall, Pass1Result, Turn

logger = get_logger("nl_sql.pipeline")

MAX_EXTRA_CALLS = 1
Executor = Callable[..., Awaitable[SqlExecutionResult | Rejection]]
UNSUPPORTED_MESSAGES = {
    "write_request": messages.WRITE_REFUSAL,
    "not_in_schema": "That information isn't available in the tables this source can read.",
    "out_of_scope": "That isn't a question this assistant can answer about this data.",
    "other": messages.GENERIC,
}


@dataclass(frozen=True)
class RepairRecord:
    reason_code: str
    original_sql: str
    attempts: int = 1


@dataclass(frozen=True)
class PipelineEvent:
    name: Literal["sql_generated", "executing", "repairing"]
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttemptRecord:
    """What one pass-1 + execution attempt came to (the eval report compares the first attempt with the final)."""

    sql: str | None
    outcome: str                    # answer | clarification | unsupported | rejected | failed | pass1_failed
    code: str | None = None
    execution: SqlExecutionResult | None = None


@dataclass(frozen=True)
class PipelineOutcome:
    kind: Literal["answer", "clarification", "unsupported", "failed"]
    envelope: Envelope | None = None
    sql: str | None = None
    execution: SqlExecutionResult | None = None
    rejection: Rejection | None = None
    repair: RepairRecord | None = None
    failure_code: str | None = None
    user_message: str | None = None
    llm_calls: tuple[LLMCall, ...] = ()
    snapshot_id: UUID | None = None
    intent_warnings: tuple[str, ...] = ()
    rewrites: tuple[str, ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()


def _same_sql(a: str, b: str) -> bool:
    """Equal after whitespace folding, or after the same parse and re-print (case and layout differences)."""
    if " ".join(a.split()) == " ".join(b.split()):
        return True
    try:
        def canon(text: str) -> str:
            tree = sqlglot.parse_one(text, read="postgres")
            return normalize_identifiers(tree, dialect="postgres").sql(dialect="postgres")

        return canon(a) == canon(b)
    except Exception:       # noqa: BLE001 - unparseable text is "different"; the validator judges it
        return False


def _call_of(p1: Pass1Result) -> tuple[LLMCall, ...]:
    return (p1.llm_call,) if p1.llm_call is not None else ()


async def _catalog_slots(
    session_factory, *, user_id: UUID, source_id: UUID, snapshot_id: UUID | None
) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    """Catalog names for message slots: ``({schema.table: columns}, [schema.table ...])``. Empty when unreadable."""
    try:
        async with session_factory() as session:
            try:
                if snapshot_id is None:
                    context = await catalog_service.get_postgres_chat_catalog_context_for_owner(
                        session, source_id=source_id, owner_user_id=user_id
                    )
                    snapshot_id = context.snapshot.id
                scope = await load_catalog_scope(
                    session, owner_user_id=user_id, source_id=source_id, snapshot_id=snapshot_id
                )
            finally:
                await session.rollback()
    except Exception:       # noqa: BLE001 - slots are a nicety; a failure here must not hide the real message
        return {}, []
    by_table = {f"{t.schema}.{t.name}": tuple(c.name for c in t.columns) for t in scope.tables}
    return by_table, list(by_table)


async def failure_text(
    c: Classification, *, session_factory, user_id: UUID, source_id: UUID, snapshot_id: UUID | None,
    envelope: Envelope | None,
) -> str:
    """The C6 message for a final failure, with slots taken from the catalog only (never from model text)."""
    if c.code not in ("column_not_found", "table_not_in_scope"):
        return messages.final_failure_message(c.code)
    by_table, table_names = await _catalog_slots(
        session_factory, user_id=user_id, source_id=source_id, snapshot_id=snapshot_id
    )
    all_columns = {col for cols in by_table.values() for col in cols}
    if c.code == "table_not_in_scope":
        suggestions = [s for s in c.suggestions if s in by_table]
        return messages.final_failure_message(c.code, tables=suggestions or table_names)
    identifier = c.identifier if c.identifier in all_columns else None
    suggestions = [s for s in c.suggestions if s in all_columns]
    if not suggestions and envelope is not None:
        used = [t for t in envelope.tables_used if t in by_table]
        suggestions = [col for t in used for col in by_table[t]][:5]
    if not suggestions:
        suggestions = list(all_columns)[:5] if by_table else []
        suggestions = sorted(suggestions)
    return messages.final_failure_message(c.code, identifier=identifier, columns=suggestions)


async def answer_question(
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
    cancellation: PostgresExecutionCancellation | None = None,
    sample_values=STORED_SAMPLES,
    on_event: Callable[[PipelineEvent], Awaitable[None]] | None = None,
    execute: Executor | None = None,
    execute_kwargs: Mapping[str, Any] | None = None,
    run: int = 0,
    allow_repair: bool = True,
) -> PipelineOutcome:
    execute = execute or execute_validated_sql
    calls: list[LLMCall] = []
    attempts: list[AttemptRecord] = []
    extra_calls = 0
    snapshot_id: UUID | None = None

    async def emit(name: str, data: dict[str, Any] | None = None) -> None:
        if on_event is not None:
            await on_event(PipelineEvent(name, data or {}))    # type: ignore[arg-type]

    def cancelled() -> bool:
        return cancellation is not None and cancellation.cancelled

    def finish(kind: str, **fields: Any) -> PipelineOutcome:
        return PipelineOutcome(
            kind=kind, llm_calls=tuple(calls), snapshot_id=snapshot_id, attempts=tuple(attempts), **fields  # type: ignore[arg-type]
        )

    def cancelled_outcome(**fields: Any) -> PipelineOutcome:
        return finish("failed", failure_code="execution_cancelled", user_message=messages.CANCELLED, **fields)

    async def failed(c: Classification, envelope: Envelope | None, **fields: Any) -> PipelineOutcome:
        text = await failure_text(
            c, session_factory=session_factory, user_id=user_id, source_id=source_id, snapshot_id=snapshot_id,
            envelope=envelope,
        )
        return finish("failed", envelope=envelope, failure_code=c.code, user_message=text, **fields)

    async def pass1(brief: str | None, attempt: int) -> Pass1Result:
        nonlocal snapshot_id
        p1 = await _cancellable(
            generate_sql(
                session_factory=session_factory, settings=settings, llm=llm, user_id=user_id, source_id=source_id,
                question=question, history=history, reference_time=reference_time, time_zone=time_zone,
                sample_values=sample_values, run=run, repair_brief=brief,
            ),
            cancellation,
        )
        calls.extend(_call_of(p1))
        snapshot_id = p1.snapshot_id or snapshot_id
        return p1

    async def run_sql(env: Envelope, attempt: int, rewrites: Sequence[str] = ()) -> SqlExecutionResult | Rejection:
        await emit("sql_generated", {
            "sql": env.sql,
            "assumptions": list(env.assumptions),
            "output_intent": env.output_intent.model_dump() if env.output_intent else None,
            "rewrites": list(rewrites),
            "attempt": attempt,
        })
        return await execute(
            session_factory=session_factory, settings=settings, user_id=user_id, source_id=source_id, sql=env.sql,
            cancellation=cancellation, on_validated=lambda: emit("executing", {"snapshot_id": str(snapshot_id) if snapshot_id else None}), **(execute_kwargs or {}),
        )

    # ── attempt 1 ─────────────────────────────────────────────────────────────────────────────────
    try:
        if cancelled():
            return cancelled_outcome()
        p1 = await pass1(None, 1)
        first = await _classify_attempt(p1, run_sql, 1, cancelled)
        attempts.append(first.record)
        if first.final is not None:
            return await _conclude(first, p1, finish, failed, cancelled_outcome)
        # Only a repairable or regenerate outcome is left.
        classification = first.classification
        assert classification is not None
        if extra_calls >= (MAX_EXTRA_CALLS if allow_repair else 0) or cancelled():
            return cancelled_outcome() if cancelled() else await failed(classification, p1.envelope)
        extra_calls += 1
        await emit("repairing", {"reason_code": classification.reason_code})
        brief = None if classification.outcome is Outcome.REGENERATE else render_brief(classification, first.previous_sql)
        p2 = await pass1(brief, 2)
        if (
            classification.outcome is Outcome.REPAIRABLE
            and p2.failure_code is None
            and p2.envelope is not None
            and p2.envelope.kind == "sql"
            and first.previous_sql
            and _same_sql(p2.envelope.sql, first.previous_sql)
        ):
            # The model sent the same query back: running it again cannot give another answer (I3).
            attempts.append(AttemptRecord(p2.envelope.sql, "same_sql", classification.code))
            return await failed(classification, p1.envelope)
        second = await _classify_attempt(p2, run_sql, 2, cancelled)
        attempts.append(second.record)
    except _TurnCancelled:
        return cancelled_outcome()

    # ── attempt 2 (the repair) ────────────────────────────────────────────────────────────────────
    if second.final is not None and second.final.kind == "answer":
        original_sql = first.previous_sql or ""
        repair = RepairRecord(reason_code=classification.reason_code, original_sql=original_sql)
        return await _conclude(second, p2, finish, failed, cancelled_outcome, repair=repair)
    if second.final is not None and second.final.kind in ("clarification", "unsupported"):
        return await _conclude(second, p2, finish, failed, cancelled_outcome)
    if second.final is not None and second.final.kind == "cancelled":
        return cancelled_outcome()
    # Failed again (or the repair produced nothing usable): the message follows the *second* error.
    last = second.classification or classification
    return await failed(last, (p2.envelope or p1.envelope))


@dataclass
class _Attempt:
    record: AttemptRecord
    final: "_Final | None" = None
    classification: Classification | None = None
    previous_sql: str | None = None


@dataclass
class _Final:
    kind: str                       # answer | clarification | unsupported | failed | cancelled
    envelope: Envelope | None = None
    execution: SqlExecutionResult | None = None
    classification: Classification | None = None


async def _classify_attempt(p1: Pass1Result, run_sql, attempt: int, cancelled) -> _Attempt:
    if p1.failure_code is not None:
        c = classify_pass1_failure(p1.failure_code, p1.failure_detail)
        record = AttemptRecord(None, "pass1_failed", p1.failure_code)
        if c.outcome is Outcome.REPAIRABLE and attempt == 1:
            return _Attempt(record, None, c, None)
        return _Attempt(record, _Final("failed", None, None, c), c)
    env = p1.envelope
    assert env is not None
    if env.kind == "clarification":
        return _Attempt(AttemptRecord(None, "clarification"), _Final("clarification", env))
    if env.kind == "unsupported":
        return _Attempt(AttemptRecord(None, "unsupported", env.reason_code), _Final("unsupported", env))
    if cancelled():
        return _Attempt(AttemptRecord(env.sql, "failed", "execution_cancelled"), _Final("cancelled", env))
    result = await run_sql(env, attempt, p1.rewrites)
    if isinstance(result, Rejection):
        c = classify_rejection(result)
        record = AttemptRecord(env.sql, "rejected", result.code.value)
        final = None if (c.outcome is Outcome.REPAIRABLE and attempt == 1) else _Final("failed", env, None, c)
        return _Attempt(record, final, c, env.sql)
    if result.is_ok:
        return _Attempt(AttemptRecord(result.sql or env.sql, "answer", None, result), _Final("answer", env, result), None, env.sql)
    if result.status == "cancelled":
        return _Attempt(AttemptRecord(env.sql, "failed", "execution_cancelled", result), _Final("cancelled", env, result))
    c = classify_execution(result)
    record = AttemptRecord(result.sql or env.sql, "failed", c.code, result)
    final = None if (c.outcome in (Outcome.REPAIRABLE, Outcome.REGENERATE) and attempt == 1) else _Final("failed", env, result, c)
    return _Attempt(record, final, c, env.sql)


async def _conclude(att: _Attempt, p1: Pass1Result, finish, failed, cancelled_outcome, repair: RepairRecord | None = None):
    fin = att.final
    assert fin is not None
    env = fin.envelope
    if fin.kind == "cancelled":
        return cancelled_outcome()
    if fin.kind == "clarification":
        # Model text, shown as plain text: at most 300 characters (the envelope rule), leak checks already applied.
        return finish("clarification", envelope=env, user_message=sanitize(env.clarification) or messages.GENERIC, rewrites=p1.rewrites)
    if fin.kind == "unsupported":
        code = env.reason_code or "other"
        return finish(
            "unsupported", envelope=env, failure_code=code,
            user_message=UNSUPPORTED_MESSAGES.get(code, messages.GENERIC), rewrites=p1.rewrites,
        )
    if fin.kind == "failed":
        c = fin.classification
        assert c is not None
        return await failed(c, env, execution=fin.execution, sql=att.record.sql)
    result = fin.execution
    assert result is not None and env is not None
    warnings = intent_warnings(env.output_intent, [(c.label, c.pg_type) for c in result.columns])
    return finish(
        "answer", envelope=env, sql=result.sql or env.sql, execution=result, repair=repair,
        intent_warnings=warnings, rewrites=p1.rewrites,
    )
