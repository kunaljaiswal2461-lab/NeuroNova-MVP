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

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
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
from app.services import metric_service, relationship_service
from app.services.postgres_query_plan import (
    PostgresApprovedJoin,
    PostgresFilter,
    PostgresGroupDimension,
    PostgresQueryPlan,
    PostgresSort,
)
from agentic_engine.conversational.prompts import postgres_query_plan as prompt
from agentic_engine.conversational.prompts import postgres_metric_query as metric_prompt
from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable
from agentic_engine.llm_engine.models import TokenUsage


_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_RELATIVE_DATE_RE = re.compile(
    r"\b(?:today|tomorrow|yesterday|this|last|next|ago|current)\b"
    r"|\b\d+\s+(?:day|days|week|weeks|month|months|year|years)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_RE = re.compile(
    r"\b(?:profil(?:e|ing)|finding|anomal(?:y|ies)|chart|graph|visuali[sz]|"
    r"correlation|distribution)\b",
    re.IGNORECASE,
)
_JOIN_REQUEST_RE = re.compile(
    r"\b(?:join|merge|combine|across\s+(?:tables|datasets|sources))\b",
    re.IGNORECASE,
)
# The relationship's join type is an owner-confirmed, immutable version fact;
# an LLM must not quietly reinterpret it from natural language.  Keep this
# recognizer deliberately tied to an *explicit* unmatched-row request rather
# than trying to infer intent from ordinary words such as "all" or "only".
_INCLUDE_UNMATCHED_BASE_ROWS_RE = re.compile(
    r"\b(?:include|including|retain|retaining|keep|keeping)\s+"
    r"(?:all\s+)?(?:unmatched|non[-\s]?matching)\s+"
    r"(?:(?:base\s+)?(?:rows?|records?)|[\w-]+)\b",
    re.IGNORECASE,
)
_EXCLUDE_UNMATCHED_BASE_ROWS_RE = re.compile(
    r"\b(?:exclude|excluding|omit|omitting|drop|dropping|remove|removing)\s+"
    r"(?:all\s+)?(?:unmatched|non[-\s]?matching)\s+"
    r"(?:(?:base\s+)?(?:rows?|records?)|[\w-]+)\b",
    re.IGNORECASE,
)
_MUTATION_COMMAND_RE = re.compile(
    r"^\s*(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)?"
    r"(?:delete|remove|update|insert|upsert|drop|truncate|create|alter|rename|"
    r"replace|grant|revoke|vacuum|analy[sz]e|reindex|cluster|copy|call|execute)\b",
    re.IGNORECASE,
)
_SQL_MUTATION_RE = re.compile(
    r"\b(?:delete\s+from|update\s+(?:only\s+)?[^\n;]{1,240}?\s+set|"
    r"insert\s+into|upsert\s+into|drop\s+(?:table|schema|database|view)|"
    r"truncate\s+(?:table\s+)?|create\s+(?:table|schema|database|view)|"
    r"alter\s+(?:table|schema|database|view)|grant\s+.+?\s+on|"
    r"revoke\s+.+?\s+on)\b",
    re.IGNORECASE,
)
_CATALOG_QUESTION_RE = re.compile(
    r"^\s*(?:"
    r"(?:list|show|describe|display)\s+(?:me\s+)?(?:the\s+)?(?:(?:available|all)\s+)?"
    r"(?:schema|catalog|metadata|tables?|columns?|fields?)\b"
    r"|(?:what|which)\s+(?:are|is|does|do)\s+(?:the\s+)?(?:available\s+)?"
    r"(?:schema|catalog|metadata|tables?|columns?|fields?)\b"
    r"|(?:what|which)\s+(?:the\s+)?(?:schema|tables?|columns?|fields?)\s+"
    r"(?:are|is|do|does|exist|in|of|for|on)\b"
    r"|(?:schema|catalog|metadata)\s+(?:for|of|in)\b"
    r"|(?:what|which)\s+(?:data|tables?|columns?|fields?)\s+(?:are|is)\s+available\b"
    r")",
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
_MAX_CANDIDATE_RELATIONSHIPS = 12
_MAX_COLUMNS = 200
_COMMENT_LIMIT = 240


class _PlanEnvelope(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["plan"]
    plan: PostgresQueryPlan


class MetricQueryIntent(BaseModel):
    """Model-selected modifiers for one already confirmed metric.

    The aggregate, asset, fixed filters, and semantic definition are absent
    on purpose.  ``metric_service`` verifies this identifier/version against
    the deterministically matched, owner-authorized definition and expands
    it into the existing ``PostgresQueryPlan`` language.
    """

    model_config = _MODEL_CONFIG

    metric_id: uuid.UUID
    version_number: StrictInt = Field(ge=1)
    # The model may select only one relationship supplied by the trusted
    # planner context.  The service rechecks every field before expansion;
    # this is never an authorization capability by itself.
    join: PostgresApprovedJoin | None = None
    additional_filters: tuple[PostgresFilter, ...] = Field(default_factory=tuple, max_length=50)
    group_by: tuple[PostgresGroupDimension, ...] = Field(default_factory=tuple, max_length=12)
    sort: PostgresSort | None = None
    limit: StrictInt = Field(ge=1, le=10000)


class _MetricPlanEnvelope(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["metric_plan"]
    intent: MetricQueryIntent


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
    # A service-resolved, trusted metric definition.  It is deliberately
    # application-only context rather than a field on PostgresQueryPlan, and
    # is revalidated by the chat agent immediately before execution.
    metric_definition: Any | None = None
    # Like ``metric_definition``, this is service-resolved application
    # context—not a planner-controlled field on the executable plan—and is
    # revalidated before execution and before result disclosure.
    relationship_definition: Any | None = None

    @property
    def executable(self) -> bool:
        return self.kind == "plan" and self.plan is not None and self.snapshot_id is not None


@dataclass(frozen=True)
class _ChosenAsset:
    asset: CatalogAsset
    columns: tuple[CatalogColumn, ...]


@dataclass(frozen=True)
class _MetricRelationshipOption:
    """One bounded current relationship plus safe lookup planning context."""

    relationship: Any
    lookup: _ChosenAsset


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


def _is_catalog_question(question: str) -> bool:
    """Recognize an explicitly informational catalog request.

    A table/column noun is also common in an ordinary calculation (for
    example, ``How many rows are in the orders table?``).  Treating every
    occurrence as metadata intent prevents valid, safe plans from reaching
    the structured planner.  Keep this deliberately narrow and let an
    ambiguous request go through normal asset selection/LLM clarification.
    """

    return bool(_CATALOG_QUESTION_RE.search(question))


def _is_write_or_ddl_request(question: str) -> bool:
    """Recognize mutation command shapes without rejecting analytical prose.

    Words such as ``updated``, ``removed``, and ``deleted`` commonly describe
    a perfectly valid row filter.  The structured plan/compiler remain the
    actual read-only execution boundary; this early check only produces a
    clearer response for recognizable DDL/DML requests.
    """

    return bool(_MUTATION_COMMAND_RE.search(question) or _SQL_MUTATION_RE.search(question))


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


def _metric_asset_payload(chosen: _ChosenAsset) -> dict[str, Any]:
    """Return only structural metadata needed for metric modifiers.

    Confirmed metric descriptions and fixed filter values are not necessary to
    choose additional filters, grouping, sorting, or a limit.  Omitting
    comments and filter values keeps this prompt surface smaller and prevents
    a metric definition from becoming an implicit data-disclosure channel.
    """

    return {
        "asset_id": str(chosen.asset.id),
        "schema_name": chosen.asset.schema_name,
        "table_name": chosen.asset.table_name,
        "columns": [
            {
                "column_id": str(column.id),
                "column_name": column.column_name,
                "native_type": column.formatted_native_type,
                "native_type_name": column.native_type_name,
                "nullable": column.is_nullable,
            }
            for column in chosen.columns
        ],
    }


def _metric_prompt_payload(resolved: Any) -> dict[str, Any]:
    """Serialize safe, definition-level context without filter values.

    ``metric_service`` owns the resolved model.  This deliberately reads only
    its documented scalar fields and an operation/column pair; it does not
    serialize a definition wholesale because that could include typed fixed
    filter values.
    """

    aggregation = getattr(resolved, "aggregation", None)
    if isinstance(aggregation, BaseModel):
        aggregation = aggregation.model_dump(mode="json")
    elif isinstance(aggregation, str):
        aggregation = {
            "operation": aggregation,
            # ``ResolvedMetricDefinition`` exposes this as
            # ``aggregation_column_id``.  Keep the legacy spelling as a
            # benign fallback for narrow test/service adapters, but never
            # infer a column from model output.
            "column_id": getattr(
                resolved,
                "aggregation_column_id",
                getattr(resolved, "aggregate_column_id", None),
            ),
        }
    elif not isinstance(aggregation, dict):
        aggregation = {
            "operation": getattr(aggregation, "operation", None),
            "column_id": getattr(aggregation, "column_id", None),
        }
    return {
        "metric_id": str(getattr(resolved, "metric_id")),
        "version_number": int(getattr(resolved, "version_number")),
        # Display spelling is intentionally preserved, but marked untrusted
        # by the metric prompt.  The service, not this text, is authoritative.
        "display_name_untrusted": str(getattr(resolved, "display_name", ""))[:256],
        "aggregation": {
            "operation": aggregation.get("operation"),
            "column_id": (
                str(aggregation["column_id"])
                if aggregation.get("column_id") is not None
                else None
            ),
        },
        "unit_label_untrusted": (
            str(getattr(resolved, "unit_label"))[:128]
            if getattr(resolved, "unit_label", None) is not None
            else None
        ),
        "fixed_filter_count": len(tuple(getattr(resolved, "fixed_filters", ()) or ())),
        "default_date_column_id": (
            str(getattr(resolved, "default_date_column_id"))
            if getattr(resolved, "default_date_column_id", None) is not None
            else None
        ),
        "default_time_zone": getattr(resolved, "default_time_zone", None),
    }


async def _metric_chosen_asset(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    context: Any,
    resolved: Any,
) -> _ChosenAsset | PostgresPlanningResult:
    """Load the exact current asset needed by an already resolved metric."""

    snapshot_id = getattr(resolved, "snapshot_id", None)
    asset_id = getattr(resolved, "asset_id", None)
    if (
        not isinstance(snapshot_id, uuid.UUID)
        or not isinstance(asset_id, uuid.UUID)
        or snapshot_id != context.snapshot.id
    ):
        return PostgresPlanningResult(
            kind="failure",
            message=(
                "The confirmed metric changed while its current catalog was being "
                "prepared. Please try the question again."
            ),
        )
    asset = next((item for item in context.assets if item.id == asset_id), None)
    if asset is None:
        return PostgresPlanningResult(
            kind="failure",
            message=(
                "The confirmed metric is no longer available in the authorized "
                "catalog. Review the metric before using it."
            ),
            snapshot_id=snapshot_id,
        )
    try:
        columns = await get_postgres_chat_asset_columns_for_owner(
            session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            asset_id=asset_id,
            owner_user_id=owner_user_id,
            limit=_MAX_COLUMNS,
        )
    except (CatalogQueryContextError, CatalogPersistenceError, NotFoundError):
        return PostgresPlanningResult(
            kind="failure",
            message=(
                "The authorized catalog changed while the confirmed metric was being "
                "prepared. Please try the question again."
            ),
        )
    return _ChosenAsset(asset=asset, columns=columns)


def _relationship_lookup_payload(option: _MetricRelationshipOption) -> dict[str, Any]:
    """Serialize only structural, current relationship facts for the LLM.

    It deliberately omits the relationship definition JSON, catalog comments,
    confirmation actor/time, bound metric filter values, and all database
    rows.  The typed IDs are still checked again by the service/compiler.
    """

    relationship = option.relationship
    pairs = getattr(relationship, "column_pairs", ()) or ()
    return {
        "relationship_id": str(getattr(relationship, "relationship_id")),
        "relationship_version_id": str(getattr(relationship, "relationship_version_id")),
        "version_number": int(getattr(relationship, "version_number")),
        "join_type": str(getattr(relationship, "join_type")),
        "base_asset_id": str(getattr(relationship, "base_asset_id")),
        "lookup_asset": _metric_asset_payload(option.lookup),
        "ordered_key_mapping": [
            {
                "base_column_name": str(getattr(pair, "base_column_name")),
                "lookup_column_name": str(getattr(pair, "lookup_column_name")),
            }
            for pair in pairs
        ],
        "target_key_kind": str(getattr(getattr(relationship, "target_key", None), "key_kind", "")),
    }


def _relationship_lookup_score(
    option: _MetricRelationshipOption,
    question_tokens: set[str],
) -> tuple[int, set[str], set[str]]:
    """Score only literal lookup names/columns, never comments or values."""

    relationship = option.relationship
    table_tokens = _tokens(
        f"{getattr(relationship, 'lookup_schema_name', '')} "
        f"{getattr(relationship, 'lookup_table_name', '')}"
    )
    column_tokens = {
        token
        for column in option.lookup.columns
        for token in _tokens(column.column_name)
    }
    table_matches = question_tokens.intersection(table_tokens)
    column_matches = question_tokens.intersection(column_tokens)
    return 3 * len(table_matches) + 2 * len(column_matches), table_matches, column_matches


def _table_name_forms(table_name: str | None) -> tuple[str, ...]:
    """Return a small literal singular/plural set for one catalog table name."""

    normalized = str(table_name or "").strip().casefold()
    if not normalized:
        return ()
    forms = {normalized}
    if normalized.endswith("ies") and len(normalized) > 3:
        forms.add(f"{normalized[:-3]}y")
    elif normalized.endswith("s") and not normalized.endswith("ss") and len(normalized) > 1:
        forms.add(normalized[:-1])
    else:
        forms.add(f"{normalized}s")
    return tuple(sorted(forms, key=len, reverse=True))


def _missing_lookup_intent_for_table_patterns(
    question: str,
    *,
    base_pattern: str,
    lookup_pattern: str,
) -> Literal["include", "exclude", "ambiguous"] | None:
    """Classify explicit missing-lookup wording for bounded table patterns."""

    subject = rf"(?:all\s+)?(?:the\s+)?{base_pattern}"
    lookup = rf"(?:{lookup_pattern})(?:\s+(?:records?|rows?))?\b"
    even_if_no = (
        r"even\s+if\s+(?:(?:there\s+(?:is|are)\s+)?no\s+"
        r"|(?:they|it|those)\s+(?:have|has)\s+no\s+)"
    )
    missing_lookup = (
        rf"(?:with\s+no\s+(?:matching\s+)?{lookup}"
        rf"|without\s+(?:(?:a|an|any|the)\s+)?(?:matching\s+)?{lookup}"
        rf"|{even_if_no}(?:matching\s+)?{lookup})"
    )
    include_verbs = "include|including|retain|retaining|keep|keeping"
    exclude_verbs = "exclude|excluding|omit|omitting|drop|dropping|remove|removing"

    def matches(verbs: str) -> bool:
        return bool(
            re.search(
                rf"\b(?:{verbs})\s+{subject}\s+{missing_lookup}",
                question,
                re.IGNORECASE,
            )
            or re.search(
                rf"{base_pattern}\s*,?\s*(?:{verbs})\s+"
                rf"(?:all\s+)?(?:them|those|(?:base\s+)?(?:rows?|records?))\s+"
                rf"{missing_lookup}",
                question,
                re.IGNORECASE,
            )
            or (
                verbs == include_verbs
                and re.search(
                    rf"{subject}\s*,?\s*{even_if_no}(?:matching\s+)?{lookup}",
                    question,
                    re.IGNORECASE,
                )
            )
        )

    includes = matches(include_verbs)
    excludes = matches(exclude_verbs)
    if includes and excludes:
        return "ambiguous"
    if includes:
        return "include"
    if excludes:
        return "exclude"
    return None


def _base_table_missing_lookup_intent(
    question: str,
    *,
    base_table_name: str | None,
) -> Literal["include", "exclude", "ambiguous"] | None:
    """Recognize a clear missing-lookup request without choosing a lookup edge.

    The base table must be named literally. The unknown target is intentionally
    not used to select any relationship; it only keeps the planner from
    falling back to a single-table plan when no eligible edge can be found.
    """

    base_forms = _table_name_forms(base_table_name)
    if not base_forms:
        return None
    return _missing_lookup_intent_for_table_patterns(
        question,
        base_pattern=rf"\b(?:{'|'.join(re.escape(item) for item in base_forms)})\b",
        lookup_pattern=r"[a-z][\w-]*",
    )


def _known_table_no_lookup_intent(
    question: str,
    *,
    base_table_name: str | None,
    lookup_table_name: str | None,
) -> Literal["include", "exclude", "ambiguous"] | None:
    """Recognize a clear missing-lookup request for one exact approved edge.

    The base and lookup names come from the current resolved relationship,
    so singular/plural spelling is the only normalization and the phrase
    cannot select an unrelated edge.
    """

    base_forms = _table_name_forms(base_table_name)
    lookup_forms = _table_name_forms(lookup_table_name)
    if not base_forms or not lookup_forms:
        return None
    return _missing_lookup_intent_for_table_patterns(
        question,
        base_pattern=rf"\b(?:{'|'.join(re.escape(item) for item in base_forms)})\b",
        lookup_pattern="|".join(re.escape(item) for item in lookup_forms),
    )


def _unmatched_base_row_intent(
    question: str,
    *,
    base_table_name: str | None = None,
    lookup_table_name: str | None = None,
) -> Literal["include", "exclude", "ambiguous"] | None:
    """Return an explicit unmatched-base-row request without semantic guessing.

    A user may ask for a join result that is incompatible with the immutable
    owner-selected relationship version.  This small lexical guard is applied
    before the model sees the relationship; it intentionally recognizes only
    clear include/exclude wording and leaves all other query semantics to the
    bounded typed-plan path.
    """

    includes = bool(_INCLUDE_UNMATCHED_BASE_ROWS_RE.search(question))
    excludes = bool(_EXCLUDE_UNMATCHED_BASE_ROWS_RE.search(question))
    known_table_intent = _known_table_no_lookup_intent(
        question,
        base_table_name=base_table_name,
        lookup_table_name=lookup_table_name,
    )
    includes = includes or known_table_intent == "include"
    excludes = excludes or known_table_intent == "exclude"
    if known_table_intent == "ambiguous":
        return "ambiguous"
    if includes and excludes:
        return "ambiguous"
    if includes:
        return "include"
    if excludes:
        return "exclude"
    return None


def _relationship_join_type_clarification(
    question: str,
    option: _MetricRelationshipOption | None,
    *,
    base_table_name: str | None,
) -> str | None:
    """Prevent a model from changing immutable INNER/LEFT completeness semantics."""

    if option is None:
        return None
    requested = _unmatched_base_row_intent(
        question,
        base_table_name=base_table_name,
        lookup_table_name=getattr(option.relationship, "lookup_table_name", None),
    )
    if requested is None:
        return None
    if requested == "ambiguous":
        return (
            "Please clarify whether unmatched base rows should be included or excluded. "
            "The approved relationship's join type is fixed for this calculation."
        )

    join_type_value = getattr(option.relationship, "join_type", "")
    join_type = str(getattr(join_type_value, "value", join_type_value)).casefold()
    if requested == "include" and join_type == "inner":
        return (
            "This approved relationship is fixed as an INNER join, which excludes "
            "unmatched base rows. Your question explicitly asks to include them. "
            "Use or confirm a LEFT relationship version, or remove that requirement."
        )
    if requested == "exclude" and join_type == "left":
        return (
            "This approved relationship is fixed as a LEFT join, which retains "
            "unmatched base rows. Your question explicitly asks to exclude them. "
            "Use or confirm an INNER relationship version, or remove that requirement."
        )
    return None


async def _relationship_option_for_metric_question(
    *,
    session: AsyncSession,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    context: Any,
    chosen: _ChosenAsset,
    question: str,
) -> _MetricRelationshipOption | PostgresPlanningResult | None:
    """Offer one demonstrably relevant approved lookup, or ask safely.

    A confirmed metric can remain a normal single-table calculation when no
    lookup wording is present.  Once the user asks for a join or names a
    lookup table/field, this selection remains intentionally conservative:
    no inferred similar-name edge, no list-position choice, and no multi-hop
    expansion.
    """

    # An explicit unmatched-base-row requirement is also a relationship
    # request. A clear base-table phrase with a missing named lookup is
    # treated the same way, but its target is not guessed or used to select
    # an edge until it exactly matches an eligible relationship below.
    missing_lookup_request = _base_table_missing_lookup_intent(
        question,
        base_table_name=chosen.asset.table_name,
    )
    explicit_join = bool(
        _JOIN_REQUEST_RE.search(question)
        or _unmatched_base_row_intent(question)
        or missing_lookup_request
    )
    try:
        relationships = await relationship_service.list_resolved_relationships_for_base_asset(
            session,
            source_id=source_id,
            owner_user_id=owner_user_id,
            base_asset_id=chosen.asset.id,
            snapshot_id=context.snapshot.id,
        )
    except Exception:
        # A relationship lookup must never make an otherwise safe single-table
        # metric execute through a fallback path.  It only becomes terminal
        # when the user expressly requested a relationship.
        if explicit_join:
            return PostgresPlanningResult(
                kind="failure",
                message=(
                    "Approved relationship context is unavailable right now, so no "
                    "joined calculation was run."
                ),
                snapshot_id=context.snapshot.id,
                asset_id=chosen.asset.id,
            )
        return None
    if not relationships:
        if explicit_join:
            return PostgresPlanningResult(
                kind="unsupported",
                message=(
                    "This confirmed metric has no approved direct lookup relationship. "
                    "Confirm a declared base-to-lookup relationship before requesting a join."
                ),
                snapshot_id=context.snapshot.id,
                asset_id=chosen.asset.id,
            )
        return None
    if len(relationships) > _MAX_CANDIDATE_RELATIONSHIPS:
        # Do not fetch arbitrary columns from an unbounded collection merely
        # to guess an edge. A user can make the lookup table explicit first.
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                "Several approved lookup relationships are available for this metric. "
                "Please name the lookup table or attribute you want to use."
            ),
            snapshot_id=context.snapshot.id,
            asset_id=chosen.asset.id,
        )

    options: list[_MetricRelationshipOption] = []
    for relationship in relationships:
        lookup_asset_id = getattr(relationship, "lookup_asset_id", None)
        lookup_asset = next(
            (asset for asset in context.assets if asset.id == lookup_asset_id),
            None,
        )
        if lookup_asset is None:
            # The resolver promised current scope. Treat any disagreement as
            # unusable rather than accepting a stale relationship response.
            continue
        try:
            lookup_columns = await get_postgres_chat_asset_columns_for_owner(
                session,
                source_id=source_id,
                snapshot_id=context.snapshot.id,
                asset_id=lookup_asset.id,
                owner_user_id=owner_user_id,
                limit=_MAX_COLUMNS,
            )
        except (CatalogQueryContextError, CatalogPersistenceError, NotFoundError):
            continue
        options.append(
            _MetricRelationshipOption(
                relationship=relationship,
                lookup=_ChosenAsset(asset=lookup_asset, columns=lookup_columns),
            )
        )
    if not options:
        return PostgresPlanningResult(
            kind="failure",
            message=(
                "The approved lookup relationship is no longer available in the "
                "authorized catalog, so no joined calculation was run."
            ),
            snapshot_id=context.snapshot.id,
            asset_id=chosen.asset.id,
        ) if explicit_join else None

    # A phrase such as "including orders with no customer" is intentionally
    # recognized only after each candidate supplies the literal current base
    # and lookup table names. That makes it an explicit request for this
    # approved edge rather than a guess from an arbitrary null-like phrase.
    no_lookup_options = [
        option
        for option in options
        if _unmatched_base_row_intent(
            question,
            base_table_name=chosen.asset.table_name,
            lookup_table_name=getattr(option.relationship, "lookup_table_name", None),
        )
        is not None
    ]
    if len(no_lookup_options) == 1:
        return no_lookup_options[0]
    if len(no_lookup_options) > 1:
        labels = ", ".join(
            f'"{getattr(item.relationship, "lookup_schema_name", "")}".'
            f'"{getattr(item.relationship, "lookup_table_name", "")}"'
            for item in no_lookup_options[:6]
        )
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                "More than one approved lookup relationship matches the explicit "
                f"unmatched-row request. Please choose one: {labels}."
            ),
            snapshot_id=context.snapshot.id,
            asset_id=chosen.asset.id,
        )
    if missing_lookup_request is not None:
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                "That unmatched-row request did not name an approved direct lookup "
                "relationship for this metric. Please name an approved lookup table "
                "or confirm the required relationship first."
            ),
            snapshot_id=context.snapshot.id,
            asset_id=chosen.asset.id,
        )

    # The base table is already fixed by the resolved metric. Do not treat a
    # repeated base-table/schema word in the metric's name as evidence that a
    # lookup relationship is wanted.
    question_tokens = _tokens(question).difference(
        _tokens(f"{chosen.asset.schema_name} {chosen.asset.table_name}")
    )
    scored = [
        (option, *_relationship_lookup_score(option, question_tokens))
        for option in options
    ]
    positive = [item for item in scored if item[1] > 0]
    if not positive:
        if len(options) == 1 and explicit_join:
            return options[0]
        if explicit_join:
            labels = ", ".join(
                f'"{getattr(item.relationship, "lookup_schema_name", "")}".'
                f'"{getattr(item.relationship, "lookup_table_name", "")}"'
                for item in options[:6]
            )
            return PostgresPlanningResult(
                kind="clarification",
                message=(
                    "Please name one approved lookup table or attribute for this metric. "
                    f"Available lookup tables include {labels}."
                ),
                snapshot_id=context.snapshot.id,
                asset_id=chosen.asset.id,
            )
        return None
    best_score = max(item[1] for item in positive)
    winners = [item for item in positive if item[1] == best_score]
    if len(winners) != 1:
        labels = ", ".join(
            f'"{getattr(item[0].relationship, "lookup_schema_name", "")}".'
            f'"{getattr(item[0].relationship, "lookup_table_name", "")}"'
            for item in winners[:6]
        )
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                "More than one approved lookup relationship could answer that metric "
                f"question. Please choose one: {labels}."
            ),
            snapshot_id=context.snapshot.id,
            asset_id=chosen.asset.id,
        )
    option, _score, table_matches, column_matches = winners[0]
    # If a bare field name exists on both tables, do not silently assume the
    # lookup side. Naming the lookup table or asking to join resolves it.
    base_column_tokens = {
        token for column in chosen.columns for token in _tokens(column.column_name)
    }
    if (
        not explicit_join
        and not table_matches
        and column_matches.intersection(base_column_tokens)
    ):
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                "That attribute appears on both the metric table and an approved lookup "
                "table. Please name the table or explicitly request the approved relationship."
            ),
            snapshot_id=context.snapshot.id,
            asset_id=chosen.asset.id,
        )
    return option


async def _match_resolved_metric(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    question: str,
) -> Any | PostgresPlanningResult | None:
    """Find a bounded, owner-authorized confirmed metric without guessing.

    A ``None`` result means ordinary structural planning should continue.
    Ambiguous/unavailable results are terminal before an LLM can select a
    semantic definition.  The small amount of duck-typing keeps this caller
    aligned with the service's deliberately narrow match result rather than
    exposing ORM rows in the conversational layer.
    """

    try:
        match = await metric_service.match_metric_definition_for_question(
            session,
            owner_user_id=owner_user_id,
            source_id=source_id,
            question=question,
        )
    except Exception:
        return PostgresPlanningResult(
            kind="failure",
            message="Confirmed metric definitions are unavailable right now, so no calculation was run.",
        )
    if match is None:
        return None
    match_kind = getattr(match, "kind", None)
    if match_kind == "ambiguous":
        return PostgresPlanningResult(
            kind="clarification",
            message=(
                str(getattr(match, "message", "")).strip()
                or "More than one confirmed metric matches that question. Please name one metric."
            ),
        )
    if match_kind != "matched":
        return PostgresPlanningResult(
            kind="failure",
            message=(
                str(getattr(match, "message", "")).strip()
                or "The confirmed metric is unavailable, so no calculation was run."
            ),
        )
    metric_id = getattr(match, "metric_id", None)
    version_number = getattr(match, "version_number", None)
    if not isinstance(metric_id, uuid.UUID) or not isinstance(version_number, int):
        return PostgresPlanningResult(
            kind="failure",
            message="The confirmed metric reference was invalid, so no calculation was run.",
        )
    try:
        return await metric_service.resolve_metric_definition_for_owner(
            session,
            owner_user_id=owner_user_id,
            source_id=source_id,
            metric_id=metric_id,
            version_number=version_number,
            require_current=True,
        )
    except Exception as exc:
        # Lifecycle exceptions have safe, intentional user outcomes.  Treat
        # every other service error as an unavailable planning context rather
        # than exposing persistence/authorization detail.
        if type(exc).__name__ == "MetricNeedsReviewError":
            return PostgresPlanningResult(
                kind="clarification",
                message="That confirmed metric needs review before it can be used.",
            )
        return PostgresPlanningResult(
            kind="failure",
            message="The confirmed metric could not be verified, so no calculation was run.",
        )


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
    # Generic Stage 4C structural planning remains deliberately single-table.
    # The only Stage 5B join path is an owner-confirmed metric expanded below.
    if plan.join is not None:
        return "Only a confirmed metric may use an approved lookup relationship."
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


def _validate_metric_join_plan(
    plan: PostgresQueryPlan,
    *,
    chosen: _ChosenAsset,
    relationship: Any,
    settings: Settings,
) -> str | None:
    """Check the deterministic expansion before the compiler re-resolves it."""

    if plan.asset_id != chosen.asset.id or plan.join is None:
        return "The joined metric plan did not retain its confirmed base table."
    if plan.limit > settings.postgres_query_max_result_limit:
        return "The requested result size exceeds the configured database-chat limit."
    if (
        plan.join.relationship_id != getattr(relationship, "relationship_id", None)
        or plan.join.relationship_version_id
        != getattr(relationship, "relationship_version_id", None)
        or plan.join.lookup_asset_id != getattr(relationship, "lookup_asset_id", None)
        or plan.join.join_type != getattr(relationship, "join_type", None)
    ):
        return "The joined metric plan did not retain its approved relationship."
    # A metric's aggregate may never move to the lookup table.  ``count_all``
    # intentionally has no column asset; it counts rows after the approved
    # join/filter semantics have been applied.
    if any(
        aggregation.column_id is not None and aggregation.asset_id != chosen.asset.id
        for aggregation in plan.aggregations
    ):
        return "The joined metric plan moved its calculation outside the confirmed base table."
    return None


async def _plan_confirmed_metric_question(
    *,
    session: AsyncSession,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    question: str,
    history: list[dict[str, str]],
    settings: Settings,
    llm: LLMClient | None,
    now: datetime | None,
    context: Any,
    resolved: Any,
) -> PostgresPlanningResult:
    """Ask for modifiers only, then expand one confirmed metric deterministically."""

    chosen = await _metric_chosen_asset(
        session,
        source_id=source_id,
        owner_user_id=owner_user_id,
        context=context,
        resolved=resolved,
    )
    if isinstance(chosen, PostgresPlanningResult):
        return chosen
    relationship_option = await _relationship_option_for_metric_question(
        session=session,
        owner_user_id=owner_user_id,
        source_id=source_id,
        context=context,
        chosen=chosen,
        question=question,
    )
    if isinstance(relationship_option, PostgresPlanningResult):
        return relationship_option
    join_type_clarification = _relationship_join_type_clarification(
        question,
        relationship_option,
        base_table_name=chosen.asset.table_name,
    )
    if join_type_clarification is not None:
        return PostgresPlanningResult(
            kind="clarification",
            message=join_type_clarification,
            snapshot_id=context.snapshot.id,
            asset_id=chosen.asset.id,
        )
    if llm is None:
        return PostgresPlanningResult(
            kind="failure",
            message="Database planning is not configured right now, so no calculation was run.",
            snapshot_id=context.snapshot.id,
            asset_id=chosen.asset.id,
        )

    reference = now or datetime.now(timezone.utc)
    usage = TokenUsage()
    try:
        llm_result = await llm.chat_json(
            model=settings.openai_mini_model,
            system=metric_prompt.SYSTEM,
            user=metric_prompt.build_user(
                question=question,
                metric=_metric_prompt_payload(resolved),
                asset=_metric_asset_payload(chosen),
                relationships=(
                    [_relationship_lookup_payload(relationship_option)]
                    if relationship_option is not None
                    else []
                ),
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
            token_usage=usage,
            asset_id=chosen.asset.id,
        )
    except Exception:
        return PostgresPlanningResult(
            kind="failure",
            message="Database planning failed safely, so no calculation was run.",
            snapshot_id=context.snapshot.id,
            token_usage=usage,
            asset_id=chosen.asset.id,
        )

    payload = llm_result.content_json
    try:
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind == "metric_plan":
            envelope = _MetricPlanEnvelope.model_validate(payload)
            if (
                envelope.intent.metric_id != getattr(resolved, "metric_id")
                or envelope.intent.version_number != getattr(resolved, "version_number")
            ):
                return PostgresPlanningResult(
                    kind="failure",
                    message="The planner selected a different confirmed metric. No calculation was run.",
                    snapshot_id=context.snapshot.id,
                    token_usage=usage,
                    asset_id=chosen.asset.id,
                )
            if envelope.intent.join is not None:
                if relationship_option is None:
                    return PostgresPlanningResult(
                        kind="failure",
                        message=(
                            "The planner selected a relationship that was not in the "
                            "authorized metric context. No calculation was run."
                        ),
                        snapshot_id=context.snapshot.id,
                        token_usage=usage,
                        asset_id=chosen.asset.id,
                    )
                plan = metric_service.expand_metric_relationship_query_intent(
                    resolved,
                    relationship_option.relationship,
                    envelope.intent,
                    settings,
                )
                validation_error = _validate_metric_join_plan(
                    plan,
                    chosen=chosen,
                    relationship=relationship_option.relationship,
                    settings=settings,
                )
            else:
                if relationship_option is not None:
                    return PostgresPlanningResult(
                        kind="failure",
                        message=(
                            "The planner did not use the approved lookup relationship "
                            "needed for that metric question. No calculation was run."
                        ),
                        snapshot_id=context.snapshot.id,
                        token_usage=usage,
                        asset_id=chosen.asset.id,
                    )
                plan = metric_service.expand_metric_query_intent(
                    resolved,
                    envelope.intent,
                    settings,
                )
                validation_error = _validate_plan_for_asset(plan, chosen, settings)
            if not isinstance(plan, PostgresQueryPlan):
                raise TypeError("metric expansion must return PostgresQueryPlan")
            if validation_error:
                return PostgresPlanningResult(
                    kind="failure",
                    message=f"{validation_error} No calculation was run.",
                    snapshot_id=context.snapshot.id,
                    token_usage=usage,
                    asset_id=chosen.asset.id,
                )
            return PostgresPlanningResult(
                kind="plan",
                message="The confirmed metric plan is ready to execute.",
                snapshot_id=context.snapshot.id,
                plan=plan,
                token_usage=usage,
                asset_id=chosen.asset.id,
                metric_definition=resolved,
                relationship_definition=(
                    relationship_option.relationship
                    if envelope.intent.join is not None and relationship_option is not None
                    else None
                ),
            )
        if kind == "clarification":
            envelope = _ClarificationEnvelope.model_validate(payload)
            return PostgresPlanningResult(
                kind="clarification",
                message=envelope.question.strip(),
                snapshot_id=context.snapshot.id,
                token_usage=usage,
                asset_id=chosen.asset.id,
            )
        if kind == "unsupported":
            envelope = _UnsupportedEnvelope.model_validate(payload)
            return PostgresPlanningResult(
                kind="unsupported",
                message=envelope.reason.strip(),
                snapshot_id=context.snapshot.id,
                token_usage=usage,
                asset_id=chosen.asset.id,
            )
    except (ValidationError, AttributeError, TypeError, ValueError):
        pass
    except Exception:
        # Metric service failures have no safe execution fallback.  Do not
        # hand a partially expanded plan to the generic planner/executor.
        pass
    return PostgresPlanningResult(
        kind="failure",
        message="The confirmed metric plan was invalid, so no calculation was run.",
        snapshot_id=context.snapshot.id,
        token_usage=usage,
        asset_id=chosen.asset.id,
    )


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

    # Reject capabilities that remain outside the bounded plan language before
    # catalog wording can accidentally mask them. A same-source join is
    # handled later only for a confirmed metric + approved direct lookup.
    if _UNSUPPORTED_RE.search(question) or _is_write_or_ddl_request(question):
        return PostgresPlanningResult(
            kind="unsupported",
            message=(
                "Database chat currently supports read-only calculations on one selected "
                "table. Write or DDL actions, profiling/findings, and charts are not available."
            ),
            snapshot_id=context.snapshot.id,
        )
    # A catalog-browsing request is never a back door to a generic join. It
    # has no confirmed metric context to preserve base-table grain.
    if _JOIN_REQUEST_RE.search(question) and _is_catalog_question(question):
        return PostgresPlanningResult(
            kind="unsupported",
            message=(
                "Write or DDL actions are not available. Database chat supports a join "
                "only through a confirmed metric and one approved direct lookup relationship."
            ),
            snapshot_id=context.snapshot.id,
        )
    if _is_catalog_question(question):
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

    matched_metric = await _match_resolved_metric(
        session,
        owner_user_id=owner_user_id,
        source_id=source_id,
        question=question,
    )
    if isinstance(matched_metric, PostgresPlanningResult):
        # The initial context was successfully owner/current-head validated,
        # so retain its snapshot only for safe presentation of a metric
        # lifecycle outcome.  The result is not executable.
        if matched_metric.snapshot_id is None:
            return PostgresPlanningResult(
                kind=matched_metric.kind,
                message=matched_metric.message,
                snapshot_id=context.snapshot.id,
                token_usage=matched_metric.token_usage,
                asset_id=matched_metric.asset_id,
            )
        return matched_metric
    if matched_metric is not None:
        return await _plan_confirmed_metric_question(
            session=session,
            owner_user_id=owner_user_id,
            source_id=source_id,
            question=question,
            history=history,
            settings=settings,
            llm=llm,
            now=now,
            context=context,
            resolved=matched_metric,
        )

    # Structural Stage 4C plans deliberately remain one-table. This is after
    # metric matching so a confirmed-metric request can reach its sole
    # approved-relationship path, but before any generic LLM plan is formed.
    if _JOIN_REQUEST_RE.search(question):
        return PostgresPlanningResult(
            kind="unsupported",
            message=(
                "Write or DDL actions are not available. Database chat supports a join "
                "only through a confirmed metric and one approved direct lookup relationship."
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
    "MetricQueryIntent",
    "PostgresPlanningResult",
    "plan_postgres_question",
    "prior_postgres_asset_id",
    "postgres_history_for_prompt",
]
