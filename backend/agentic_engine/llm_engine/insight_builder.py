"""Insight orchestrator — Layer 5 entry point.

Runs three LLM calls in parallel against a single dataset snapshot:

  1. ExecutiveSummary       — narrative overview         (gpt-4o)
  2. FindingExplanation[]   — per-finding plain English  (gpt-4o-mini, batched)
  3. SuggestedQuestion[]    — chat starters              (gpt-4o-mini)

Failure modes are absorbed by the orchestrator: if any single call fails
or the client is unavailable, the corresponding section degrades to an
empty placeholder so the pipeline can still reach COMPLETE.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from agentic_engine.findings.models import FindingsReport
from agentic_engine.llm_engine.base_llm import ChatResult, LLMClient, LLMUnavailable
from agentic_engine.llm_engine.models import (
    ExecutiveSummary,
    FindingExplanation,
    InsightReport,
    SuggestedQuestion,
    TokenUsage,
)
from agentic_engine.llm_engine.openai_client import build_client
from agentic_engine.llm_engine.prompt_builder import (
    build_dataset_context,
    select_findings_for_explanation,
    serialise_findings_for_prompt,
)
from agentic_engine.llm_engine.prompts import (
    executive_summary as exec_prompt,
    finding_explanation as exp_prompt,
    suggested_questions as q_prompt,
)
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("llm.insight_builder")


# Token budgets are intentionally tight — the outputs are JSON, not prose.
_MAX_TOKENS_SUMMARY = 1200
_MAX_TOKENS_EXPLANATIONS = 2400
_MAX_TOKENS_QUESTIONS = 900

# Hard cap on findings sent to the explanation prompt — protects against
# token blowup on pathological datasets with dozens of HIGH/MEDIUM findings.
_MAX_FINDINGS_TO_EXPLAIN = 25


# ── public entry point ──────────────────────────────────────────────────────

async def build_insights(
    profile: ProfileReport,
    findings: FindingsReport,
    *,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> InsightReport:
    """Generate the InsightReport for a dataset.

    Args:
        profile:  the upstream ProfileReport (Layer 2).
        findings: the upstream FindingsReport (Layer 3).
        llm:      optional LLM client; if not provided, one is built from
                  ``settings.openai_api_key``. Pass an explicit client in
                  tests to inject a stub.
        settings: optional override for the global Settings.

    The function never raises on LLM failure — instead it returns an
    InsightReport with ``degraded=True`` so the pipeline keeps moving.
    """
    settings = settings or get_settings()
    client = llm if llm is not None else build_client(settings.openai_api_key)

    if client is None:
        logger.warning(
            "llm.degraded.no_api_key",
            dataset_id=str(profile.dataset_id),
        )
        return _degraded_report(
            profile,
            reason="OPENAI_API_KEY is not configured",
            model_used=settings.openai_chat_model,
        )

    context = build_dataset_context(profile, findings)
    findings_for_explanation = (
        select_findings_for_explanation(findings)[:_MAX_FINDINGS_TO_EXPLAIN]
    )

    summary_task = _call_summary(client, settings, context)
    explanations_task = _call_explanations(client, settings, findings_for_explanation)
    questions_task = _call_questions(client, settings, context)

    summary_res, explanations_res, questions_res = await asyncio.gather(
        summary_task,
        explanations_task,
        questions_task,
        return_exceptions=True,
    )

    token_usage = TokenUsage()
    degraded = False
    degraded_reasons: list[str] = []

    summary, _used_summary = _unpack(
        summary_res,
        empty=_empty_summary(),
        token_usage=token_usage,
        section="executive_summary",
        degraded_reasons=degraded_reasons,
    )
    explanations, _used_exp = _unpack(
        explanations_res,
        empty=[],
        token_usage=token_usage,
        section="finding_explanations",
        degraded_reasons=degraded_reasons,
    )
    questions, _used_q = _unpack(
        questions_res,
        empty=[],
        token_usage=token_usage,
        section="suggested_questions",
        degraded_reasons=degraded_reasons,
    )

    if degraded_reasons:
        degraded = True

    report = InsightReport(
        dataset_id=profile.dataset_id,
        executive_summary=summary,
        finding_explanations=explanations,
        suggested_questions=questions,
        model_used=settings.openai_chat_model,
        token_usage=token_usage,
        degraded=degraded,
        degraded_reason="; ".join(degraded_reasons) if degraded_reasons else None,
    )

    logger.info(
        "llm.insight_built",
        dataset_id=str(profile.dataset_id),
        explanations=report.explanation_count,
        questions=report.question_count,
        input_tokens=token_usage.input_tokens,
        output_tokens=token_usage.output_tokens,
        degraded=degraded,
    )
    return report


# ── individual call wrappers ────────────────────────────────────────────────

async def _call_summary(
    client: LLMClient,
    settings: Settings,
    context: dict[str, Any],
) -> tuple[ExecutiveSummary, ChatResult]:
    result = await client.chat_json(
        model=settings.openai_chat_model,
        system=exec_prompt.SYSTEM,
        user=exec_prompt.build_user(context),
        max_tokens=_MAX_TOKENS_SUMMARY,
        temperature=0.2,
    )
    summary = ExecutiveSummary.model_validate(result.content_json)
    return summary, result


async def _call_explanations(
    client: LLMClient,
    settings: Settings,
    findings_subset,
) -> tuple[list[FindingExplanation], ChatResult | None]:
    if not findings_subset:
        return [], None

    payload = serialise_findings_for_prompt(findings_subset)
    result = await client.chat_json(
        model=settings.openai_mini_model,
        system=exp_prompt.SYSTEM,
        user=exp_prompt.build_user(payload),
        max_tokens=_MAX_TOKENS_EXPLANATIONS,
        temperature=0.2,
    )
    raw = result.content_json.get("explanations", [])
    parsed: list[FindingExplanation] = []
    valid_ids = {str(f.finding_id) for f in findings_subset}
    for item in raw:
        try:
            exp = FindingExplanation.model_validate(item)
        except ValidationError as exc:
            logger.warning("llm.explanation_invalid", item=item, error=str(exc))
            continue
        # Defend against the model hallucinating a finding_id that was
        # never in the input — drop it rather than letting it propagate.
        if str(exp.finding_id) not in valid_ids:
            logger.warning(
                "llm.explanation_unknown_id",
                finding_id=str(exp.finding_id),
            )
            continue
        parsed.append(exp)
    return parsed, result


async def _call_questions(
    client: LLMClient,
    settings: Settings,
    context: dict[str, Any],
) -> tuple[list[SuggestedQuestion], ChatResult]:
    result = await client.chat_json(
        model=settings.openai_mini_model,
        system=q_prompt.SYSTEM,
        user=q_prompt.build_user(context),
        max_tokens=_MAX_TOKENS_QUESTIONS,
        temperature=0.4,
    )
    raw = result.content_json.get("questions", [])
    parsed: list[SuggestedQuestion] = []
    for item in raw:
        try:
            parsed.append(SuggestedQuestion.model_validate(item))
        except ValidationError as exc:
            logger.warning("llm.question_invalid", item=item, error=str(exc))
    return parsed, result


# ── helpers ─────────────────────────────────────────────────────────────────

def _unpack(
    result: Any,
    *,
    empty: Any,
    token_usage: TokenUsage,
    section: str,
    degraded_reasons: list[str],
):
    """Convert a ``gather(return_exceptions=True)`` slot into (value, ChatResult).

    On exception or LLMUnavailable, log the reason, append it to
    ``degraded_reasons``, and return the supplied empty fallback.
    """
    if isinstance(result, BaseException):
        reason = f"{section}: {type(result).__name__}: {result}"
        logger.warning("llm.section_failed", section=section, error=str(result))
        degraded_reasons.append(reason)
        return empty, None

    value, chat_result = result
    if chat_result is not None:
        token_usage.add(chat_result.input_tokens, chat_result.output_tokens)
    return value, chat_result


def _empty_summary() -> ExecutiveSummary:
    return ExecutiveSummary(
        headline="Insights unavailable",
        overview=(
            "The LLM Insight layer could not generate a narrative summary "
            "for this dataset. Findings and visualisations remain available."
        ),
        key_strengths=[],
        key_concerns=[],
        recommended_next_steps=[],
    )


def _degraded_report(
    profile: ProfileReport,
    *,
    reason: str,
    model_used: str,
) -> InsightReport:
    return InsightReport(
        dataset_id=profile.dataset_id,
        executive_summary=_empty_summary(),
        finding_explanations=[],
        suggested_questions=[],
        model_used=model_used,
        token_usage=TokenUsage(),
        degraded=True,
        degraded_reason=reason,
    )
