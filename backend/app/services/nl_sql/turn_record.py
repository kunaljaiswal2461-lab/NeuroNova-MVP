"""Build what a SQL-path turn stores and sends (SPEC-09 C1, C2). Pure functions."""
from __future__ import annotations

from typing import Any

from app.schemas.nl_sql_record import (
    LLMUsage,
    NlSqlTurn,
    Pass2Info,
    PresentationRecord,
    RepairModel,
    TableColumn,
)

from .pass2 import Pass2Outcome
from .pass2_validate import mandatory_caveats
from .pipeline import PipelineOutcome
from .prompt import PASS2_PROMPT_VERSION, PROMPT_VERSION

EMPTY_ENCODINGS: dict[str, Any] = {"x": None, "y": [], "series": None, "label": None, "value": None}


def presentation_record(
    pass2: Pass2Outcome, *, summary: str, consent: bool, complete: bool, repaired: bool
) -> PresentationRecord:
    """The resolved spec, or (on any pass-2 failure) the templated summary with the table."""
    inp = pass2.input
    info = Pass2Info(
        consent=consent,
        masked_columns=list(inp.masked_columns) if inp is not None else [],
        rows_sent=inp.rows_sent if inp is not None else 0,
        budget_cut=inp.budget_cut if inp is not None else None,
    )
    resolved = pass2.resolved
    if resolved is None:
        return PresentationRecord(
            display="table", encodings=dict(EMPTY_ENCODINGS), table_columns=[], answer_template=summary, answer=summary,
            takeaways_template=[], takeaways=[],
            caveats=mandatory_caveats(complete=complete, masked=False, consent=consent, repaired=repaired),
            fallback=pass2.fallback or "spec_invalid", pass2=info,
        )
    return PresentationRecord(
        display=resolved.display,
        encodings=resolved.encodings,
        table_columns=[TableColumn(**c) for c in resolved.table_columns],
        answer_template=resolved.answer_template,
        answer=resolved.answer,
        takeaways_template=resolved.takeaways_template,
        takeaways=resolved.takeaways,
        caveats=resolved.caveats,
        fallback=resolved.downgrade,
        derived=resolved.derived,
        pass2=info,
    )


def sse_presentation(record: PresentationRecord) -> dict[str, Any]:
    """The ``presentation`` event payload: the resolved spec. Templates, placeholders and pass-2 internals stay out."""
    return {
        "answer": record.answer,
        "takeaways": record.takeaways,
        "caveats": record.caveats,
        "display": record.display,
        "encodings": record.encodings,
        "table_columns": [c.model_dump() for c in record.table_columns],
        "fallback": record.fallback,
        "derived": record.derived,
    }


def build_turn_record(
    *,
    route: str,
    outcome: PipelineOutcome,
    presentation: PresentationRecord | None,
    llm_calls: tuple[Any, ...],
    pass2_ran: bool,
) -> NlSqlTurn:
    envelope = outcome.envelope
    sql = outcome.sql
    if sql is None and outcome.attempts:
        sql = next((a.sql for a in reversed(outcome.attempts) if a.sql), None)
    versions = {"pass1": PROMPT_VERSION}
    if pass2_ran:
        versions["pass2"] = PASS2_PROMPT_VERSION
    return NlSqlTurn(
        route=route,                       # type: ignore[arg-type]
        prompt_versions=versions,
        sql=sql,
        assumptions=list(envelope.assumptions) if envelope is not None else [],
        output_intent=envelope.output_intent.model_dump() if envelope is not None and envelope.output_intent else None,
        rewrites=list(outcome.rewrites),
        intent_warnings=list(outcome.intent_warnings),
        repair=(
            RepairModel(reason_code=outcome.repair.reason_code, original_sql=outcome.repair.original_sql,
                        attempts=outcome.repair.attempts)
            if outcome.repair is not None else None
        ),
        outcome=outcome.kind,
        failure_code=outcome.failure_code,
        presentation=presentation,
        llm=LLMUsage(
            calls=len(llm_calls),
            input_tokens=sum(c.input_tokens for c in llm_calls),
            output_tokens=sum(c.output_tokens for c in llm_calls),
        ),
    )
