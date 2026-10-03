"""Bounded natural-language planning for one authorized PostgreSQL source.

This module deliberately stops before execution.  It selects a catalog asset
deterministically, asks the LLM for a *structured* Stage 4A plan only, and
pins the current catalog snapshot for the caller.  It never accepts or emits
SQL, credentials, raw database rows, or a user-controlled source/snapshot.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.catalog import CatalogAsset, CatalogColumn
from app.exceptions.custom_exceptions import NotFoundError
from app.services.catalog_service import (
    CatalogPersistenceError,
    CatalogQueryContextError,
    get_postgres_chat_asset_columns_for_owner,
    get_postgres_chat_catalog_context_for_owner,
)
from app.services.postgres_query_plan import PostgresQueryPlan
from agentic_engine.conversational.prompts import postgres_query_plan as prompt
from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable
from agentic_engine.llm_engine.models import TokenUsage


_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_RELATIVE_DATE_RE = re.compile(
    r"\b(?:today|tomorrow|yesterday|this|last|next|ago|current)\b"
    r"|\b\d+\s+(?:day|days|week|weeks|month|months|year|years)\b",
    re.IGNORECASE,
)
_SCHEMA_RE = re.compile(
    r"\b(?:schema|catalog|metadata|columns?|fields?|tables?|data\s+available|"
    r"column\s+types?)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_RE = re.compile(
    r"\b(?:join|merge|combine|across\s+(?:tables|datasets|sources)|"
    r"profil(?:e|ing)|finding|anomal(?:y|ies)|chart|graph|visuali[sz]|"
    r"correlation|distribution)\b",
    re.IGNORECASE,
)
_BUSINESS_METRIC_RE = re.compile(
    r"\b(?:revenue|profit|margin|sales|income)\b", re.IGNORECASE
)
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "by", "calculate", "can", "count", "data",
        "for", "from", "give", "how", "i", "in", "is", "list", "me", "of",
        "on", "please", "records", "rows", "show", "the", "to", "what", "with",
    }
)
_MAX_CANDIDATE_ASSETS = 12
_MAX_COLUMNS = 200
_COMMENT_LIMIT = 240


class _PlanEnvelope(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["plan"]
    plan: PostgresQueryPlan


class _ClarificationEnvelope(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["clarification"]
    question: str = Field(min_length=1, max_length=400)


class _UnsupportedEnvelope(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["unsupported"]
    reason: str = Field(min_length=1, max_length=400)


@dataclass(frozen=True)
class PostgresPlanningResult:
    """A safe, non-executable outcome for one database-chat turn."""

    kind: Literal["plan", "clarification", "unsupported", "catalog", "failure"]
    message: str
    snapshot_id: uuid.UUID | None = None
    plan: PostgresQueryPlan | None = None
    token_usage: TokenUsage | None = None
    asset_id: uuid.UUID | None = None

    @property
    def executable(self) -> bool:
        return self.kind == "plan" and self.plan is not None and self.snapshot_id is not None


@dataclass(frozen=True)
class _ChosenAsset:
    asset: CatalogAsset
    columns: tuple[CatalogColumn, ...]


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(value)
        if len(token) > 1 and token.casefold() not in _STOPWORDS
    }


def _asset_score(asset: CatalogAsset, tokens: set[str]) -> int:
    name_tokens = _tokens(f"{asset.schema_name} {asset.table_name}")
    return sum(3 for token in tokens if token in name_tokens)


def _column_score(columns: tuple[CatalogColumn, ...], tokens: set[str]) -> int:
    names = {_token for column in columns for _token in _tokens(column.column_name)}
    return sum(2 for token in tokens if token in names)


def _candidate_labels(assets: list[CatalogAsset], *, max_items: int = 6) -> str:
    labels = [f'"{asset.schema_name}"."{asset.table_name}"' for asset in assets[:max_items]]
    suffix = "" if len(assets) <= max_items else ", …"
    return ", ".join(labels) + suffix


async def _choose_asset(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    assets: tuple[CatalogAsset, ...],
    question: str,
    prior_asset_id: uuid.UUID | None = None,
) -> _ChosenAsset | PostgresPlanningResult:
    """Use deterministic, staged name matching; never use list position."""

    if len(assets) == 1:
        asset = assets[0]
        columns = await get_postgres_chat_asset_columns_for_owner(
            session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            asset_id=asset.id,
            owner_user_id=owner_user_id,
            limit=_MAX_COLUMNS,
        )
        return _ChosenAsset(asset=asset, columns=columns)

    question_tokens = _tokens(question)
    table_scores = {asset.id: _asset_score(asset, question_tokens) for asset in assets}
    ranked = sorted(
        assets,
        key=lambda asset: (-table_scores[asset.id], asset.schema_name, asset.table_name, str(asset.id)),
    )
    best_score = table_scores[ranked[0].id]
    table_winners = [asset for asset in ranked if table_scores[asset.id] == best_score]
    if best_score > 0 and len(table_winners) == 1:
        asset = table_winners[0]
        columns = await get_postgres_chat_asset_columns_for_owner(
            session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            asset_id=asset.id,
            owner_user_id=owner_user_id,
            limit=_MAX_COLUMNS,
        )
        return _ChosenAsset(asset=asset, columns=columns)

    # Names alone are often not enough (e.g. "total amount by region").
    # Expand only an explicitly bounded candidate set and use column *names*,
    # never comments, to disambiguate.
    candidates = table_winners if best_score > 0 else list(ranked)
    if len(candidates) > _MAX_CANDIDATE_ASSETS:
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                "Please name one table before I run a database calculation. "
                f"Available candidates include {_candidate_labels(candidates)}."
            ),
            snapshot_id=snapshot_id,
        )

    loaded: list[_ChosenAsset] = []
    for asset in candidates:
        columns = await get_postgres_chat_asset_columns_for_owner(
            session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            asset_id=asset.id,
            owner_user_id=owner_user_id,
            limit=_MAX_COLUMNS,
        )
        loaded.append(_ChosenAsset(asset=asset, columns=columns))
    column_scores = {
        candidate.asset.id: _column_score(candidate.columns, question_tokens)
        for candidate in loaded
    }
    best_column_score = max(column_scores.values(), default=0)
    winners = [
        candidate for candidate in loaded
        if column_scores[candidate.asset.id] == best_column_score
    ]
    if best_column_score > 0 and len(winners) == 1:
        return winners[0]
    # A prior successful result is trusted application provenance, not user
    # prompt text.  It is useful for bounded follow-ups such as “now group it
    # by month”, but only after this turn has failed to name a more decisive
    # current table/column and only after the current-head/scope helper has
    # proved the old asset remains selected.
    if prior_asset_id is not None:
        for candidate in loaded:
            if candidate.asset.id == prior_asset_id:
                return candidate
    return PostgresPlanningResult(
        kind="clarification",
        message=(
            "I need the table for this calculation; I will not choose one "
            f"implicitly. Please specify one of {_candidate_labels([item.asset for item in loaded])}."
        ),
        snapshot_id=snapshot_id,
    )


def _safe_comment(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("\x00", "")[:_COMMENT_LIMIT]


def _asset_payload(chosen: _ChosenAsset) -> dict[str, Any]:
    return {
        "asset_id": str(chosen.asset.id),
        "schema_name": chosen.asset.schema_name,
        "table_name": chosen.asset.table_name,
        "table_comment_untrusted": _safe_comment(chosen.asset.table_comment),
        "columns": [
            {
                "column_id": str(column.id),
                "column_name": column.column_name,
                "native_type": column.formatted_native_type,
                "native_type_name": column.native_type_name,
                "nullable": column.is_nullable,
                "column_comment_untrusted": _safe_comment(column.column_comment),
            }
            for column in chosen.columns
        ],
    }


def _schema_message(assets: tuple[CatalogAsset, ...], snapshot_id: uuid.UUID) -> str:
    labels = _candidate_labels(list(assets), max_items=20)
    return (
        f"The current authorized catalog snapshot ({snapshot_id}) contains "
        f"{len(assets)} selected table(s): {labels}. Ask for one table's columns "
        "or a single-table calculation."
    )


def _asset_schema_message(chosen: _ChosenAsset, snapshot_id: uuid.UUID) -> str:
    columns = ", ".join(
        f'"{column.column_name}" ({column.formatted_native_type})'
        for column in chosen.columns
    )
    return (
        f'The current authorized snapshot ({snapshot_id}) has table '
        f'"{chosen.asset.schema_name}"."{chosen.asset.table_name}" with columns: {columns}. '
        "Database comments are descriptive only, not business definitions."
    )


def _required_business_metric_column(
    question: str, chosen: _ChosenAsset
) -> uuid.UUID | PostgresPlanningResult | None:
    """Avoid treating common business words as a catalog semantic definition.

    An exact matching column name is a literal user/canonical-schema choice,
    not a semantic inference.  Anything less specific requires clarification.
    """

    terms = {match.casefold() for match in _BUSINESS_METRIC_RE.findall(question)}
    if not terms:
        return None
    exact_matches = [
        column for column in chosen.columns
        if column.column_name.casefold() in terms
    ]
    if len(exact_matches) != 1:
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                "Please name the exact catalog column and calculation for that business "
                "metric. Database chat does not infer a revenue, profit, margin, sales, "
                "or income definition from names or comments."
            ),
            asset_id=chosen.asset.id,
        )
    return exact_matches[0].id


def _validate_plan_for_asset(
    plan: PostgresQueryPlan,
    chosen: _ChosenAsset,
    settings: Settings,
    required_business_metric_column: uuid.UUID | None = None,
) -> str | None:
    if plan.asset_id != chosen.asset.id:
        return "The planner selected a table outside the authorized context."
    if plan.limit > settings.postgres_query_max_result_limit:
        return "The requested result size exceeds the configured database-chat limit."
    permitted = {column.id for column in chosen.columns}
    references = [projection.column_id for projection in plan.projections]
    references.extend(filter_item.column_id for filter_item in plan.filters)
    references.extend(dimension.column_id for dimension in plan.group_by)
    references.extend(
        aggregation.column_id
        for aggregation in plan.aggregations
        if aggregation.column_id is not None
    )
    if any(column_id not in permitted for column_id in references):
        return "The planner selected a column outside the authorized table context."
    if required_business_metric_column is not None:
        output_columns = {projection.column_id for projection in plan.projections}
        output_columns.update(
            aggregation.column_id
            for aggregation in plan.aggregations
            if aggregation.column_id is not None
        )
        if required_business_metric_column not in output_columns:
            return "The planner did not use the exact business-metric column requested."
    return None


def postgres_history_for_prompt(messages: list[Any]) -> list[dict[str, str]]:
    """Bound history while ensuring result rows never re-enter a prompt."""

    history: list[dict[str, str]] = []
    for message in messages[-24:]:
        # Stage 4C result data can be retained for transcript presentation;
        # it is never planner context.  Assistant prose without a result can
        # still carry a prior clarification/unsupported limitation.
        if getattr(message, "postgres_result", None) is not None:
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if content:
            role = getattr(message, "role", None)
            history.append({
                "role": "assistant" if getattr(role, "value", role) == "ASSISTANT" else "user",
                "content": content[:1000],
            })
    return history[-12:]


def prior_postgres_asset_id(messages: list[Any]) -> uuid.UUID | None:
    """Return only a trusted, bounded prior asset reference for follow-ups."""

    for message in reversed(messages[-24:]):
        result = getattr(message, "postgres_result", None)
        if result is None or getattr(result, "status", None) != "succeeded":
            continue
        asset_id = getattr(result, "asset_id", None)
        if isinstance(asset_id, uuid.UUID):
            return asset_id
    return None


async def plan_postgres_question(
    *,
    session: AsyncSession,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    question: str,
    history: list[dict[str, str]],
    settings: Settings,
    llm: LLMClient | None,
    now: datetime | None = None,
    prior_asset_id: uuid.UUID | None = None,
) -> PostgresPlanningResult:
    """Return a safe plan/clarification/unsupported outcome for one source."""

    try:
        context = await get_postgres_chat_catalog_context_for_owner(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
    except (CatalogQueryContextError, CatalogPersistenceError, NotFoundError):
        return PostgresPlanningResult(
            kind="failure",
            message=(
                "The current authorized catalog is unavailable for database chat. "
                "Refresh metadata or select another source before trying again."
            ),
        )

    if _SCHEMA_RE.search(question):
        try:
            schema_asset = await _choose_asset(
                session,
                source_id=source_id,
                snapshot_id=context.snapshot.id,
                owner_user_id=owner_user_id,
                assets=context.assets,
                question=question,
                prior_asset_id=prior_asset_id,
            )
        except (CatalogQueryContextError, CatalogPersistenceError, NotFoundError):
            return PostgresPlanningResult(
                kind="failure",
                message="The authorized catalog changed while the schema question was being prepared.",
            )
        if isinstance(schema_asset, _ChosenAsset):
            return PostgresPlanningResult(
                kind="catalog",
                message=_asset_schema_message(schema_asset, context.snapshot.id),
                snapshot_id=context.snapshot.id,
                asset_id=schema_asset.asset.id,
            )
        return PostgresPlanningResult(
            kind="catalog",
            message=_schema_message(context.assets, context.snapshot.id),
            snapshot_id=context.snapshot.id,
        )
    if _UNSUPPORTED_RE.search(question):
        return PostgresPlanningResult(
            kind="unsupported",
            message=(
                "Database chat currently supports read-only calculations on one selected "
                "table. Joins, profiling/findings, and charts are not available."
            ),
            snapshot_id=context.snapshot.id,
        )
    if _RELATIVE_DATE_RE.search(question):
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                "Please provide an explicit ISO-8601 date or timestamp and, for "
                "timestamp values, an IANA time zone. Database chat does not infer "
                "a business or browser time zone for relative dates."
            ),
            snapshot_id=context.snapshot.id,
        )

    try:
        selected = await _choose_asset(
            session,
            source_id=source_id,
            snapshot_id=context.snapshot.id,
            owner_user_id=owner_user_id,
            assets=context.assets,
            question=question,
            prior_asset_id=prior_asset_id,
        )
    except (CatalogQueryContextError, CatalogPersistenceError, NotFoundError):
        return PostgresPlanningResult(
            kind="failure",
            message="The authorized catalog changed while the question was being prepared. Please try again.",
        )
    if isinstance(selected, PostgresPlanningResult):
        return selected

    business_metric = _required_business_metric_column(question, selected)
    if isinstance(business_metric, PostgresPlanningResult):
        return PostgresPlanningResult(
            kind=business_metric.kind,
            message=business_metric.message,
            snapshot_id=context.snapshot.id,
            asset_id=selected.asset.id,
        )

    if llm is None:
        return PostgresPlanningResult(
            kind="failure",
            message="Database planning is not configured right now, so no calculation was run.",
            snapshot_id=context.snapshot.id,
            asset_id=selected.asset.id,
        )

    reference = now or datetime.now(timezone.utc)
    usage = TokenUsage()
    try:
        llm_result = await llm.chat_json(
            model=settings.openai_mini_model,
            system=prompt.SYSTEM,
            user=prompt.build_user(
                question=question,
                asset=_asset_payload(selected),
                history=history,
                reference_clock=reference.astimezone(timezone.utc).isoformat(),
                reference_time_zone="UTC",
                max_limit=settings.postgres_query_max_result_limit,
            ),
            max_tokens=900,
            temperature=0.0,
        )
        usage.add(llm_result.input_tokens, llm_result.output_tokens)
    except LLMUnavailable:
        return PostgresPlanningResult(
            kind="failure",
            message="Database planning is temporarily unavailable, so no calculation was run.",
            snapshot_id=context.snapshot.id,
            asset_id=selected.asset.id,
        )
    except Exception:
        # Do not surface provider details, prompts, or model payloads in a
        # database stream.  A failed plan is not a reason to execute anything.
        return PostgresPlanningResult(
            kind="failure",
            message="Database planning failed safely, so no calculation was run.",
            snapshot_id=context.snapshot.id,
            asset_id=selected.asset.id,
        )

    payload = llm_result.content_json
    try:
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind == "plan":
            envelope = _PlanEnvelope.model_validate(payload)
            validation_error = _validate_plan_for_asset(
                envelope.plan,
                selected,
                settings,
                required_business_metric_column=business_metric,
            )
            if validation_error:
                return PostgresPlanningResult(
                    kind="failure",
                    message=f"{validation_error} No calculation was run.",
                    snapshot_id=context.snapshot.id,
                    token_usage=usage,
                    asset_id=selected.asset.id,
                )
            return PostgresPlanningResult(
                kind="plan",
                message="The authorized plan is ready to execute.",
                snapshot_id=context.snapshot.id,
                plan=envelope.plan,
                token_usage=usage,
                asset_id=selected.asset.id,
            )
        if kind == "clarification":
            envelope = _ClarificationEnvelope.model_validate(payload)
            return PostgresPlanningResult(
                kind="clarification",
                message=envelope.question.strip(),
                snapshot_id=context.snapshot.id,
                token_usage=usage,
                asset_id=selected.asset.id,
            )
        if kind == "unsupported":
            envelope = _UnsupportedEnvelope.model_validate(payload)
            return PostgresPlanningResult(
                kind="unsupported",
                message=envelope.reason.strip(),
                snapshot_id=context.snapshot.id,
                token_usage=usage,
                asset_id=selected.asset.id,
            )
    except (ValidationError, AttributeError, TypeError, ValueError):
        pass
    return PostgresPlanningResult(
        kind="failure",
        message="The database plan was invalid, so no calculation was run.",
        snapshot_id=context.snapshot.id,
        token_usage=usage,
        asset_id=selected.asset.id,
    )


__all__ = [
    "PostgresPlanningResult",
    "plan_postgres_question",
    "prior_postgres_asset_id",
    "postgres_history_for_prompt",
]
