"""Confirmed, bounded same-source relationship definition boundary.

Stage 5B deliberately approves only one catalog-declared foreign-key
direction at a time: a base (referencing) table joins a uniquely keyed lookup
(referenced) table.  No caller can submit table names, column mappings, ON
expressions, inferred relationships, or cross-source references.  Every use
is rematerialized against the current owner-authorized catalog before a
planner/compiler may construct a typed join.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.catalog import (
    CatalogAsset,
    CatalogColumn,
    CatalogDeclaredKey,
    CatalogDeclaredKeyColumn,
    CatalogForeignKey,
    CatalogForeignKeyColumn,
    CatalogForeignKeyTargetStatus,
    CatalogKeyKind,
)
from app.db.models.relationship_definition import (
    RelationshipDefinition,
    RelationshipDefinitionVersion,
    RelationshipJoinType,
    RelationshipLifecycleState,
)
from app.exceptions.custom_exceptions import AppError, NotFoundError
from app.schemas.source_schemas import RelationshipDefinitionWriteRequest
from app.services.catalog_service import (
    CatalogPersistenceError,
    CatalogQueryContextError,
    PostgresChatCatalogContext,
    get_postgres_chat_catalog_context_for_owner,
)
from app.services.source_service import get_source_for_owner


class RelationshipServiceError(AppError):
    pass


class RelationshipValidationError(RelationshipServiceError):
    status_code = 422
    code = "invalid_relationship_definition"

    def __init__(self, message: str = "relationship definition is invalid"):
        super().__init__(message)


class RelationshipConflictError(RelationshipServiceError):
    status_code = 409
    code = "relationship_definition_conflict"

    def __init__(self, message: str = "relationship definition conflicts with current source state"):
        super().__init__(message)


class RelationshipNeedsReviewError(RelationshipServiceError):
    status_code = 409
    code = "relationship_definition_needs_review"

    def __init__(self, message: str = "relationship definition needs owner review before it can be used"):
        super().__init__(message)


class RelationshipRetiredError(RelationshipServiceError):
    status_code = 409
    code = "relationship_definition_retired"

    def __init__(self):
        super().__init__("relationship definition is retired and cannot be used")


class RelationshipCatalogUnavailableError(RelationshipServiceError):
    status_code = 409
    code = "relationship_catalog_unavailable"

    def __init__(self):
        super().__init__("the current authorized catalog is unavailable for this relationship")


class RelationshipPersistenceError(RelationshipServiceError):
    status_code = 503
    code = "relationship_definition_persistence_failed"

    def __init__(self):
        super().__init__("could not complete the relationship definition request")


class _RelationshipRequirementError(Exception):
    """The current catalog no longer meets an immutable approved contract."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RelationshipColumnPair:
    ordinal_position: int
    base_column_id: uuid.UUID
    base_column_name: str
    lookup_column_id: uuid.UUID
    lookup_column_name: str


@dataclass(frozen=True)
class ResolvedRelationshipColumnPair(RelationshipColumnPair):
    """A current, typed equality pair usable only by the typed join plan."""

    base_native_type_schema: str | None
    base_native_type_name: str | None
    base_native_type_oid: int | None
    base_native_type_modifier: int | None
    base_is_nullable: bool
    lookup_native_type_schema: str | None
    lookup_native_type_name: str | None
    lookup_native_type_oid: int | None
    lookup_native_type_modifier: int | None
    lookup_is_nullable: bool


@dataclass(frozen=True)
class ResolvedRelationshipTargetKey:
    key_id: uuid.UUID
    constraint_name: str
    key_kind: Literal["primary_key", "unique"]
    lookup_column_ids: tuple[uuid.UUID, ...]
    lookup_column_names: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedRelationshipDefinition:
    relationship_id: uuid.UUID
    relationship_version_id: uuid.UUID
    version_number: int
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    foreign_key_id: uuid.UUID
    # Preserve the declared FK identity separately from the target key.  The
    # executor rechecks this exact, ordered mapping on the remote PostgreSQL
    # relation after both tables are locked.
    foreign_key_constraint_name: str
    base_asset_id: uuid.UUID
    base_schema_name: str
    base_table_name: str
    lookup_asset_id: uuid.UUID
    lookup_schema_name: str
    lookup_table_name: str
    join_type: Literal["inner", "left"]
    column_pairs: tuple[ResolvedRelationshipColumnPair, ...]
    target_key: ResolvedRelationshipTargetKey
    confirmed_by_user_id: uuid.UUID
    confirmed_at: datetime
    allowed_base_column_ids: frozenset[uuid.UUID]
    allowed_lookup_column_ids: frozenset[uuid.UUID]


@dataclass(frozen=True)
class RelationshipCurrentCatalogView:
    snapshot_id: uuid.UUID
    foreign_key_id: uuid.UUID
    base_asset_id: uuid.UUID
    base_schema_name: str
    base_table_name: str
    lookup_asset_id: uuid.UUID
    lookup_schema_name: str
    lookup_table_name: str
    join_type: Literal["inner", "left"]
    column_pairs: tuple[RelationshipColumnPair, ...]
    target_key: ResolvedRelationshipTargetKey


@dataclass(frozen=True)
class RelationshipDefinitionView:
    relationship_id: uuid.UUID
    version_number: int
    snapshot_id: uuid.UUID
    foreign_key_id: uuid.UUID
    base_asset_id: uuid.UUID
    base_schema_name: str | None
    base_table_name: str | None
    lookup_asset_id: uuid.UUID
    lookup_schema_name: str | None
    lookup_table_name: str | None
    join_type: Literal["inner", "left"]
    column_pairs: tuple[RelationshipColumnPair, ...]
    target_key_id: uuid.UUID
    target_key_constraint_name: str | None
    target_key_kind: Literal["primary_key", "unique"] | None
    confirmed_by_user_id: uuid.UUID
    confirmed_at: datetime
    state: Literal["valid", "needs_review", "retired"]
    needs_review_reason: str | None
    retired_at: datetime | None
    current_catalog: RelationshipCurrentCatalogView | None = None


@dataclass(frozen=True)
class EligibleDeclaredRelationship:
    """A current, owner-visible declared FK that is eligible for confirmation."""

    snapshot_id: uuid.UUID
    foreign_key_id: uuid.UUID
    constraint_name: str
    base_asset_id: uuid.UUID
    base_schema_name: str
    base_table_name: str
    lookup_asset_id: uuid.UUID
    lookup_schema_name: str
    lookup_table_name: str
    column_pairs: tuple[RelationshipColumnPair, ...]
    target_key: ResolvedRelationshipTargetKey


@dataclass(frozen=True)
class RelationshipProvenance:
    """Safe relationship facts for a higher-level run/audit presentation."""

    relationship_id: uuid.UUID
    relationship_version_id: uuid.UUID
    version_number: int
    base_table_name: str
    lookup_table_name: str
    join_type: Literal["inner", "left"]
    target_key_kind: Literal["primary_key", "unique"]
    target_key_constraint_name: str
    column_pair_count: int


@dataclass(frozen=True)
class _DeclaredRelationshipCandidate:
    context: PostgresChatCatalogContext
    foreign_key: CatalogForeignKey
    base_asset: CatalogAsset
    lookup_asset: CatalogAsset
    mappings: tuple[tuple[CatalogForeignKeyColumn, CatalogColumn, CatalogColumn], ...]
    target_key: CatalogDeclaredKey
    target_key_columns: tuple[CatalogColumn, ...]


_STRUCTURAL_KEYS = (
    "column_name",
    "native_type_schema",
    "native_type_name",
    "native_type_oid",
    "native_type_modifier",
    "is_nullable",
)
_MAX_RELATIONSHIP_COLUMN_PAIRS = 16
_MAX_RELATIONSHIP_DEFINITIONS = 200
_MAX_ELIGIBLE_RELATIONSHIPS = 500
_MAX_COLUMNS_PER_RELATIONSHIP_ASSET = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _join_type_from_enum(value: RelationshipJoinType) -> Literal["inner", "left"]:
    return "inner" if value is RelationshipJoinType.INNER else "left"


def _join_type_enum(value: str) -> RelationshipJoinType:
    try:
        return RelationshipJoinType(value.upper())
    except (AttributeError, ValueError) as exc:
        raise RelationshipValidationError("relationship join type is invalid") from exc


def _key_kind_public(value: CatalogKeyKind) -> Literal["primary_key", "unique"]:
    if value is CatalogKeyKind.PRIMARY_KEY:
        return "primary_key"
    if value is CatalogKeyKind.UNIQUE:
        return "unique"
    raise _RelationshipRequirementError("target_key_invalid")


def _column_contract(column: CatalogColumn) -> dict[str, Any]:
    return {
        "column_id": str(column.id),
        "column_name": column.column_name,
        "native_type_schema": column.native_type_schema,
        "native_type_name": column.native_type_name,
        "native_type_oid": column.native_type_oid,
        "native_type_modifier": column.native_type_modifier,
        "is_nullable": column.is_nullable,
    }


def _column_from_contract(
    contract: object,
    *,
    columns_by_name: dict[str, CatalogColumn],
) -> CatalogColumn:
    if not isinstance(contract, dict):
        raise _RelationshipRequirementError("definition_invalid")
    name = contract.get("column_name")
    if not isinstance(name, str):
        raise _RelationshipRequirementError("definition_invalid")
    column = columns_by_name.get(name)
    if column is None:
        raise _RelationshipRequirementError("column_missing")
    for key in _STRUCTURAL_KEYS:
        if contract.get(key) != getattr(column, key):
            raise _RelationshipRequirementError("column_changed")
    return column


def _columns_support_equality(base_column: CatalogColumn, lookup_column: CatalogColumn) -> bool:
    """Keep the first join dialect intentionally strict and deterministic.

    A PostgreSQL FK can in some cases use coercible-but-not-identical types.
    Stage 5B does not encode a type-coercion language, so it accepts only
    exact discovered native type identity and modifier pairs.  The live
    executor still rechecks the actual constraint before remote execution.
    """

    return (
        base_column.native_type_oid is not None
        and lookup_column.native_type_oid is not None
        and base_column.native_type_schema == lookup_column.native_type_schema
        and base_column.native_type_name == lookup_column.native_type_name
        and base_column.native_type_oid == lookup_column.native_type_oid
        and base_column.native_type_modifier == lookup_column.native_type_modifier
    )


def _identity_fingerprint(
    *, base_asset: CatalogAsset, lookup_asset: CatalogAsset, foreign_key: CatalogForeignKey
) -> str:
    # This preserves PostgreSQL's exact identifier semantics.  Do not fold
    # case or strip punctuation; quoted identifiers can make those distinct.
    canonical = json.dumps(
        {
            "base": [base_asset.schema_name, base_asset.table_name],
            "foreign_key": foreign_key.constraint_name,
            "lookup": [lookup_asset.schema_name, lookup_asset.table_name],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _display_name(
    *, base_asset: CatalogAsset, lookup_asset: CatalogAsset, foreign_key: CatalogForeignKey
) -> str:
    return (
        f"{base_asset.schema_name}.{base_asset.table_name} → "
        f"{lookup_asset.schema_name}.{lookup_asset.table_name} "
        f"({foreign_key.constraint_name})"
    )[:255]


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        pass


async def _current_context(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> PostgresChatCatalogContext:
    try:
        return await get_postgres_chat_catalog_context_for_owner(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
    except (CatalogQueryContextError, CatalogPersistenceError):
        raise RelationshipCatalogUnavailableError() from None
    except NotFoundError:
        # Preserve the established source-scoped non-enumerating behavior.
        raise NotFoundError("source not found") from None


async def _mappings_for_foreign_key(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    foreign_key: CatalogForeignKey,
) -> tuple[tuple[CatalogForeignKeyColumn, CatalogColumn, CatalogColumn], ...]:
    if foreign_key.referenced_asset_id is None:
        raise _RelationshipRequirementError("target_out_of_scope")
    base_column = aliased(CatalogColumn)
    lookup_column = aliased(CatalogColumn)
    try:
        rows = (
            await session.execute(
                select(CatalogForeignKeyColumn, base_column, lookup_column)
                .join(
                    base_column,
                    (base_column.source_id == CatalogForeignKeyColumn.source_id)
                    & (base_column.snapshot_id == CatalogForeignKeyColumn.snapshot_id)
                    & (base_column.asset_id == CatalogForeignKeyColumn.referencing_asset_id)
                    & (base_column.id == CatalogForeignKeyColumn.referencing_column_id),
                )
                .join(
                    lookup_column,
                    (lookup_column.source_id == CatalogForeignKeyColumn.source_id)
                    & (lookup_column.snapshot_id == CatalogForeignKeyColumn.snapshot_id)
                    & (lookup_column.asset_id == CatalogForeignKeyColumn.referenced_asset_id)
                    & (lookup_column.id == CatalogForeignKeyColumn.referenced_column_id),
                )
                .where(
                    CatalogForeignKeyColumn.source_id == source_id,
                    CatalogForeignKeyColumn.snapshot_id == snapshot_id,
                    CatalogForeignKeyColumn.foreign_key_id == foreign_key.id,
                    CatalogForeignKeyColumn.referencing_asset_id == foreign_key.referencing_asset_id,
                    CatalogForeignKeyColumn.referenced_asset_id == foreign_key.referenced_asset_id,
                )
                .order_by(CatalogForeignKeyColumn.ordinal_position)
                .limit(_MAX_RELATIONSHIP_COLUMN_PAIRS + 1)
            )
        ).all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    if not rows or len(rows) > _MAX_RELATIONSHIP_COLUMN_PAIRS:
        raise _RelationshipRequirementError("foreign_key_mapping_unsupported")
    result = tuple(rows)
    if [item[0].ordinal_position for item in result] != list(range(1, len(result) + 1)):
        raise _RelationshipRequirementError("foreign_key_mapping_invalid")
    if any(not _columns_support_equality(base, lookup) for _, base, lookup in result):
        raise _RelationshipRequirementError("foreign_key_column_type_unsupported")
    return result


async def _columns_for_key(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    asset_id: uuid.UUID,
    key: CatalogDeclaredKey,
) -> tuple[CatalogColumn, ...]:
    try:
        columns = (
            await session.execute(
                select(CatalogColumn)
                .join(
                    CatalogDeclaredKeyColumn,
                    (CatalogDeclaredKeyColumn.source_id == CatalogColumn.source_id)
                    & (CatalogDeclaredKeyColumn.snapshot_id == CatalogColumn.snapshot_id)
                    & (CatalogDeclaredKeyColumn.asset_id == CatalogColumn.asset_id)
                    & (CatalogDeclaredKeyColumn.column_id == CatalogColumn.id),
                )
                .where(
                    CatalogDeclaredKeyColumn.source_id == source_id,
                    CatalogDeclaredKeyColumn.snapshot_id == snapshot_id,
                    CatalogDeclaredKeyColumn.asset_id == asset_id,
                    CatalogDeclaredKeyColumn.key_id == key.id,
                )
                .order_by(CatalogDeclaredKeyColumn.ordinal_position)
                .limit(_MAX_RELATIONSHIP_COLUMN_PAIRS + 1)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    if not columns or len(columns) > _MAX_RELATIONSHIP_COLUMN_PAIRS:
        raise _RelationshipRequirementError("target_key_unsupported")
    return tuple(columns)


async def _current_columns_for_assets(
    session: AsyncSession,
    *,
    context: PostgresChatCatalogContext,
    base_asset_id: uuid.UUID,
    lookup_asset_id: uuid.UUID,
) -> tuple[tuple[CatalogColumn, ...], tuple[CatalogColumn, ...]]:
    """Return the full bounded current column surface for both approved roles.

    The FK pairs remain the only permitted ON predicates.  The complete
    owner-authorized surfaces are returned separately so a typed plan can use
    lookup attributes for grouping/filtering and preserve base-table metric
    fields without treating either as an extra join condition.
    """

    try:
        columns = (
            await session.execute(
                select(CatalogColumn)
                .where(
                    CatalogColumn.source_id == context.source_id,
                    CatalogColumn.snapshot_id == context.snapshot.id,
                    CatalogColumn.asset_id.in_((base_asset_id, lookup_asset_id)),
                )
                .order_by(CatalogColumn.asset_id, CatalogColumn.ordinal_position, CatalogColumn.id)
                .limit((_MAX_COLUMNS_PER_RELATIONSHIP_ASSET * 2) + 1)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    by_asset: dict[uuid.UUID, list[CatalogColumn]] = {base_asset_id: [], lookup_asset_id: []}
    for column in columns:
        if column.asset_id in by_asset:
            by_asset[column.asset_id].append(column)
    base_columns = tuple(by_asset[base_asset_id])
    lookup_columns = tuple(by_asset[lookup_asset_id])
    if (
        not base_columns
        or not lookup_columns
        or len(base_columns) > _MAX_COLUMNS_PER_RELATIONSHIP_ASSET
        or len(lookup_columns) > _MAX_COLUMNS_PER_RELATIONSHIP_ASSET
    ):
        raise _RelationshipRequirementError("asset_columns_unavailable")
    return base_columns, lookup_columns


async def _matching_target_key(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    lookup_asset_id: uuid.UUID,
    lookup_column_ids: tuple[uuid.UUID, ...],
    required_constraint_name: str | None = None,
    required_kind: CatalogKeyKind | None = None,
) -> tuple[CatalogDeclaredKey, tuple[CatalogColumn, ...]]:
    conditions = [
        CatalogDeclaredKey.source_id == source_id,
        CatalogDeclaredKey.snapshot_id == snapshot_id,
        CatalogDeclaredKey.asset_id == lookup_asset_id,
        CatalogDeclaredKey.key_kind.in_((CatalogKeyKind.PRIMARY_KEY, CatalogKeyKind.UNIQUE)),
    ]
    if required_constraint_name is not None:
        conditions.append(CatalogDeclaredKey.constraint_name == required_constraint_name)
    if required_kind is not None:
        conditions.append(CatalogDeclaredKey.key_kind == required_kind)
    try:
        keys = (
            await session.execute(
                select(CatalogDeclaredKey)
                .where(*conditions)
                .order_by(
                    case(
                        (CatalogDeclaredKey.key_kind == CatalogKeyKind.PRIMARY_KEY, 0),
                        else_=1,
                    ),
                    CatalogDeclaredKey.constraint_name,
                    CatalogDeclaredKey.id,
                )
                .limit(_MAX_RELATIONSHIP_COLUMN_PAIRS + 8)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    for key in keys:
        columns = await _columns_for_key(
            session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            asset_id=lookup_asset_id,
            key=key,
        )
        if tuple(column.id for column in columns) == lookup_column_ids:
            return key, columns
    raise _RelationshipRequirementError("target_key_missing")


async def _candidate_for_foreign_key(
    session: AsyncSession,
    *,
    context: PostgresChatCatalogContext,
    foreign_key_id: uuid.UUID | None = None,
    constraint_name: str | None = None,
    base_asset: CatalogAsset | None = None,
    lookup_asset: CatalogAsset | None = None,
    required_target_key_name: str | None = None,
    required_target_key_kind: CatalogKeyKind | None = None,
) -> _DeclaredRelationshipCandidate:
    """Resolve one current in-scope FK and exact unique target evidence."""

    asset_by_id = {asset.id: asset for asset in context.assets}
    conditions = [
        CatalogForeignKey.source_id == context.source_id,
        CatalogForeignKey.snapshot_id == context.snapshot.id,
        CatalogForeignKey.target_status == CatalogForeignKeyTargetStatus.IN_SCOPE,
        CatalogForeignKey.referenced_asset_id.is_not(None),
    ]
    if foreign_key_id is not None:
        conditions.append(CatalogForeignKey.id == foreign_key_id)
    if constraint_name is not None:
        conditions.append(CatalogForeignKey.constraint_name == constraint_name)
    if base_asset is not None:
        conditions.append(CatalogForeignKey.referencing_asset_id == base_asset.id)
    if lookup_asset is not None:
        conditions.append(CatalogForeignKey.referenced_asset_id == lookup_asset.id)
    try:
        foreign_keys = (
            await session.execute(
                select(CatalogForeignKey)
                .where(*conditions)
                .order_by(CatalogForeignKey.constraint_name, CatalogForeignKey.id)
                .limit(2)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    if len(foreign_keys) != 1:
        raise _RelationshipRequirementError("foreign_key_missing")
    foreign_key = foreign_keys[0]
    selected_base = asset_by_id.get(foreign_key.referencing_asset_id)
    selected_lookup = asset_by_id.get(foreign_key.referenced_asset_id)
    if selected_base is None or selected_lookup is None:
        raise _RelationshipRequirementError("asset_unavailable")
    if selected_base.id == selected_lookup.id:
        raise _RelationshipRequirementError("self_join_unsupported")
    mappings = await _mappings_for_foreign_key(
        session,
        source_id=context.source_id,
        snapshot_id=context.snapshot.id,
        foreign_key=foreign_key,
    )
    target_key, target_key_columns = await _matching_target_key(
        session,
        source_id=context.source_id,
        snapshot_id=context.snapshot.id,
        lookup_asset_id=selected_lookup.id,
        lookup_column_ids=tuple(mapping[2].id for mapping in mappings),
        required_constraint_name=required_target_key_name,
        required_kind=required_target_key_kind,
    )
    return _DeclaredRelationshipCandidate(
        context=context,
        foreign_key=foreign_key,
        base_asset=selected_base,
        lookup_asset=selected_lookup,
        mappings=mappings,
        target_key=target_key,
        target_key_columns=target_key_columns,
    )


def _public_pairs(
    mappings: tuple[tuple[CatalogForeignKeyColumn, CatalogColumn, CatalogColumn], ...]
) -> tuple[RelationshipColumnPair, ...]:
    return tuple(
        RelationshipColumnPair(
            ordinal_position=mapping.ordinal_position,
            base_column_id=base_column.id,
            base_column_name=base_column.column_name,
            lookup_column_id=lookup_column.id,
            lookup_column_name=lookup_column.column_name,
        )
        for mapping, base_column, lookup_column in mappings
    )


def _resolved_pairs(
    mappings: tuple[tuple[CatalogForeignKeyColumn, CatalogColumn, CatalogColumn], ...]
) -> tuple[ResolvedRelationshipColumnPair, ...]:
    return tuple(
        ResolvedRelationshipColumnPair(
            ordinal_position=mapping.ordinal_position,
            base_column_id=base_column.id,
            base_column_name=base_column.column_name,
            lookup_column_id=lookup_column.id,
            lookup_column_name=lookup_column.column_name,
            base_native_type_schema=base_column.native_type_schema,
            base_native_type_name=base_column.native_type_name,
            base_native_type_oid=base_column.native_type_oid,
            base_native_type_modifier=base_column.native_type_modifier,
            base_is_nullable=base_column.is_nullable,
            lookup_native_type_schema=lookup_column.native_type_schema,
            lookup_native_type_name=lookup_column.native_type_name,
            lookup_native_type_oid=lookup_column.native_type_oid,
            lookup_native_type_modifier=lookup_column.native_type_modifier,
            lookup_is_nullable=lookup_column.is_nullable,
        )
        for mapping, base_column, lookup_column in mappings
    )


def _target_key_view(
    key: CatalogDeclaredKey, columns: tuple[CatalogColumn, ...]
) -> ResolvedRelationshipTargetKey:
    return ResolvedRelationshipTargetKey(
        key_id=key.id,
        constraint_name=key.constraint_name,
        key_kind=_key_kind_public(key.key_kind),
        lookup_column_ids=tuple(column.id for column in columns),
        lookup_column_names=tuple(column.column_name for column in columns),
    )


def _definition_payload(
    candidate: _DeclaredRelationshipCandidate,
    *,
    join_type: Literal["inner", "left"],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "base_asset": {
            "schema_name": candidate.base_asset.schema_name,
            "table_name": candidate.base_asset.table_name,
        },
        "lookup_asset": {
            "schema_name": candidate.lookup_asset.schema_name,
            "table_name": candidate.lookup_asset.table_name,
        },
        "foreign_key": {"constraint_name": candidate.foreign_key.constraint_name},
        "target_key": {
            "constraint_name": candidate.target_key.constraint_name,
            "key_kind": candidate.target_key.key_kind.value,
            "columns": [_column_contract(column) for column in candidate.target_key_columns],
        },
        "column_pairs": [
            {
                "ordinal_position": mapping.ordinal_position,
                "base_column": _column_contract(base_column),
                "lookup_column": _column_contract(lookup_column),
            }
            for mapping, base_column, lookup_column in candidate.mappings
        ],
        "join_type": join_type,
    }


async def _validate_confirmation_request(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: RelationshipDefinitionWriteRequest,
) -> tuple[_DeclaredRelationshipCandidate, dict[str, Any]]:
    if request.owner_confirmed is not True:
        raise RelationshipValidationError("owner confirmation is required")
    context = await _current_context(
        session, source_id=source_id, owner_user_id=owner_user_id
    )
    if context.snapshot.id != request.snapshot_id:
        raise RelationshipConflictError(
            "catalog snapshot changed; reload the declared relationship before confirming"
        )
    try:
        candidate = await _candidate_for_foreign_key(
            session,
            context=context,
            foreign_key_id=request.foreign_key_id,
        )
    except _RelationshipRequirementError as exc:
        raise RelationshipValidationError("declared relationship is not eligible for confirmation") from exc
    join_type = _join_type_from_enum(_join_type_enum(request.join_type))
    return candidate, _definition_payload(candidate, join_type=join_type)


async def _definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    lock: bool = False,
) -> RelationshipDefinition:
    statement = select(RelationshipDefinition).where(
        RelationshipDefinition.id == relationship_id,
        RelationshipDefinition.source_id == source_id,
        RelationshipDefinition.owner_user_id == owner_user_id,
    )
    if lock:
        statement = statement.with_for_update()
    try:
        definition = (await session.execute(statement)).scalar_one_or_none()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    if definition is None:
        await _rollback_quietly(session)
        raise NotFoundError("relationship definition not found")
    return definition


async def _version_for_definition(
    session: AsyncSession,
    *,
    definition: RelationshipDefinition,
    version_number: int | None = None,
    relationship_version_id: uuid.UUID | None = None,
) -> RelationshipDefinitionVersion:
    if version_number is None and relationship_version_id is None:
        raise RelationshipValidationError("relationship version is required")
    conditions = [
        RelationshipDefinitionVersion.source_id == definition.source_id,
        RelationshipDefinitionVersion.relationship_definition_id == definition.id,
    ]
    if version_number is not None:
        conditions.append(RelationshipDefinitionVersion.version_number == version_number)
    if relationship_version_id is not None:
        conditions.append(RelationshipDefinitionVersion.id == relationship_version_id)
    try:
        version = (await session.execute(select(RelationshipDefinitionVersion).where(*conditions))).scalar_one_or_none()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    if version is None:
        await _rollback_quietly(session)
        raise NotFoundError("relationship definition version not found")
    return version


async def _active_identity_conflict(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    identity_fingerprint: str,
    except_relationship_id: uuid.UUID | None = None,
) -> bool:
    statement = select(RelationshipDefinition.id).where(
        RelationshipDefinition.source_id == source_id,
        RelationshipDefinition.identity_fingerprint == identity_fingerprint,
        RelationshipDefinition.lifecycle_state == RelationshipLifecycleState.ACTIVE,
    )
    if except_relationship_id is not None:
        statement = statement.where(RelationshipDefinition.id != except_relationship_id)
    try:
        return (await session.execute(statement.limit(1))).scalar_one_or_none() is not None
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None


def _payload_identity(payload: object) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise _RelationshipRequirementError("definition_invalid")
    base = payload.get("base_asset")
    lookup = payload.get("lookup_asset")
    foreign_key = payload.get("foreign_key")
    target_key = payload.get("target_key")
    pairs = payload.get("column_pairs")
    if (
        not isinstance(base, dict)
        or not isinstance(lookup, dict)
        or not isinstance(foreign_key, dict)
        or not isinstance(target_key, dict)
        or not isinstance(pairs, list)
        or not pairs
        or len(pairs) > _MAX_RELATIONSHIP_COLUMN_PAIRS
    ):
        raise _RelationshipRequirementError("definition_invalid")
    if not all(isinstance(item.get("schema_name"), str) and isinstance(item.get("table_name"), str) for item in (base, lookup)):
        raise _RelationshipRequirementError("definition_invalid")
    if not isinstance(foreign_key.get("constraint_name"), str):
        raise _RelationshipRequirementError("definition_invalid")
    return base, lookup, foreign_key, target_key, pairs


async def _rematerialize_version(
    session: AsyncSession,
    *,
    definition: RelationshipDefinition,
    version: RelationshipDefinitionVersion,
    owner_user_id: uuid.UUID,
) -> ResolvedRelationshipDefinition:
    context = await _current_context(
        session, source_id=definition.source_id, owner_user_id=owner_user_id
    )
    base_payload, lookup_payload, foreign_key_payload, target_key_payload, pairs_payload = _payload_identity(
        version.definition_json
    )
    base_asset = next(
        (
            asset
            for asset in context.assets
            if asset.schema_name == base_payload["schema_name"]
            and asset.table_name == base_payload["table_name"]
        ),
        None,
    )
    lookup_asset = next(
        (
            asset
            for asset in context.assets
            if asset.schema_name == lookup_payload["schema_name"]
            and asset.table_name == lookup_payload["table_name"]
        ),
        None,
    )
    if base_asset is None or lookup_asset is None or base_asset.id == lookup_asset.id:
        raise _RelationshipRequirementError("asset_unavailable")
    key_kind_raw = target_key_payload.get("key_kind")
    try:
        expected_key_kind = CatalogKeyKind(key_kind_raw)
    except (TypeError, ValueError) as exc:
        raise _RelationshipRequirementError("definition_invalid") from exc
    try:
        candidate = await _candidate_for_foreign_key(
            session,
            context=context,
            constraint_name=foreign_key_payload["constraint_name"],
            base_asset=base_asset,
            lookup_asset=lookup_asset,
            required_target_key_name=target_key_payload.get("constraint_name"),
            required_target_key_kind=expected_key_kind,
        )
    except _RelationshipRequirementError:
        raise

    target_columns_payload = target_key_payload.get("columns")
    if not isinstance(target_columns_payload, list) or len(target_columns_payload) != len(candidate.target_key_columns):
        raise _RelationshipRequirementError("definition_invalid")
    current_lookup_columns_by_name = {
        lookup_column.column_name
        for _, _, lookup_column in candidate.mappings
    }
    if len(current_lookup_columns_by_name) != len(candidate.mappings):
        raise _RelationshipRequirementError("foreign_key_mapping_invalid")
    for stored, current in zip(target_columns_payload, candidate.target_key_columns, strict=True):
        # Key evidence must remain exact.  A constraint with a same-name but
        # different column order is not silently accepted.
        resolved = _column_from_contract(
            stored,
            columns_by_name={column.column_name: column for column in candidate.target_key_columns},
        )
        if resolved.id != current.id:
            raise _RelationshipRequirementError("target_key_changed")

    if len(pairs_payload) != len(candidate.mappings):
        raise _RelationshipRequirementError("foreign_key_mapping_changed")
    for expected_ordinal, (stored_pair, current_pair) in enumerate(
        zip(pairs_payload, candidate.mappings, strict=True), start=1
    ):
        mapping, base_column, lookup_column = current_pair
        if (
            not isinstance(stored_pair, dict)
            or stored_pair.get("ordinal_position") != expected_ordinal
            or mapping.ordinal_position != expected_ordinal
        ):
            raise _RelationshipRequirementError("foreign_key_mapping_changed")
        stored_base = _column_from_contract(
            stored_pair.get("base_column"),
            columns_by_name={base_column.column_name: base_column},
        )
        stored_lookup = _column_from_contract(
            stored_pair.get("lookup_column"),
            columns_by_name={lookup_column.column_name: lookup_column},
        )
        if stored_base.id != base_column.id or stored_lookup.id != lookup_column.id:
            raise _RelationshipRequirementError("foreign_key_mapping_changed")
    join_type = _join_type_from_enum(version.join_type)
    if version.definition_json.get("join_type") != join_type:
        raise _RelationshipRequirementError("definition_invalid")
    base_columns, lookup_columns = await _current_columns_for_assets(
        session,
        context=context,
        base_asset_id=candidate.base_asset.id,
        lookup_asset_id=candidate.lookup_asset.id,
    )
    return ResolvedRelationshipDefinition(
        relationship_id=definition.id,
        relationship_version_id=version.id,
        version_number=version.version_number,
        source_id=definition.source_id,
        snapshot_id=context.snapshot.id,
        foreign_key_id=candidate.foreign_key.id,
        foreign_key_constraint_name=candidate.foreign_key.constraint_name,
        base_asset_id=candidate.base_asset.id,
        base_schema_name=candidate.base_asset.schema_name,
        base_table_name=candidate.base_asset.table_name,
        lookup_asset_id=candidate.lookup_asset.id,
        lookup_schema_name=candidate.lookup_asset.schema_name,
        lookup_table_name=candidate.lookup_asset.table_name,
        join_type=join_type,
        column_pairs=_resolved_pairs(candidate.mappings),
        target_key=_target_key_view(candidate.target_key, candidate.target_key_columns),
        confirmed_by_user_id=version.confirmed_by_user_id,
        confirmed_at=version.confirmed_at,
        # These are complete role-scoped catalog surfaces, not ON-predicate
        # permissions. ``column_pairs`` remains the sole approved equality
        # mapping. Planner/compiler can therefore support a lookup dimension
        # such as customers.region without turning it into another join key.
        allowed_base_column_ids=frozenset(column.id for column in base_columns),
        allowed_lookup_column_ids=frozenset(column.id for column in lookup_columns),
    )


async def _mark_needs_review(
    session: AsyncSession,
    *,
    definition: RelationshipDefinition,
    reason: str,
) -> None:
    if definition.lifecycle_state is not RelationshipLifecycleState.ACTIVE:
        return
    definition.lifecycle_state = RelationshipLifecycleState.NEEDS_REVIEW
    definition.needs_review_at = _now()
    definition.needs_review_reason = reason[:64]
    try:
        await session.commit()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None


def _state_for(
    definition: RelationshipDefinition,
) -> Literal["valid", "needs_review", "retired"]:
    if definition.lifecycle_state is RelationshipLifecycleState.RETIRED:
        return "retired"
    if definition.lifecycle_state is RelationshipLifecycleState.NEEDS_REVIEW:
        return "needs_review"
    return "valid"


def _historical_pairs(version: RelationshipDefinitionVersion) -> tuple[RelationshipColumnPair, ...]:
    try:
        _, _, _, _, raw_pairs = _payload_identity(version.definition_json)
    except _RelationshipRequirementError:
        return ()
    pairs: list[RelationshipColumnPair] = []
    for raw in raw_pairs:
        if not isinstance(raw, dict):
            continue
        base = raw.get("base_column")
        lookup = raw.get("lookup_column")
        if not isinstance(base, dict) or not isinstance(lookup, dict):
            continue
        try:
            pairs.append(
                RelationshipColumnPair(
                    ordinal_position=int(raw["ordinal_position"]),
                    base_column_id=uuid.UUID(str(base["column_id"])),
                    base_column_name=str(base["column_name"]),
                    lookup_column_id=uuid.UUID(str(lookup["column_id"])),
                    lookup_column_name=str(lookup["column_name"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(pairs)


def _view_from_rows(
    definition: RelationshipDefinition,
    version: RelationshipDefinitionVersion,
    *,
    resolved: ResolvedRelationshipDefinition | None = None,
) -> RelationshipDefinitionView:
    payload = version.definition_json if isinstance(version.definition_json, dict) else {}
    base = payload.get("base_asset") if isinstance(payload, dict) else None
    lookup = payload.get("lookup_asset") if isinstance(payload, dict) else None
    target_key = payload.get("target_key") if isinstance(payload, dict) else None
    return RelationshipDefinitionView(
        relationship_id=definition.id,
        version_number=version.version_number,
        snapshot_id=version.confirmed_snapshot_id,
        foreign_key_id=version.foreign_key_id,
        base_asset_id=version.base_asset_id,
        base_schema_name=base.get("schema_name") if isinstance(base, dict) else None,
        base_table_name=base.get("table_name") if isinstance(base, dict) else None,
        lookup_asset_id=version.lookup_asset_id,
        lookup_schema_name=lookup.get("schema_name") if isinstance(lookup, dict) else None,
        lookup_table_name=lookup.get("table_name") if isinstance(lookup, dict) else None,
        join_type=_join_type_from_enum(version.join_type),
        column_pairs=_historical_pairs(version),
        target_key_id=version.target_key_id,
        target_key_constraint_name=(
            target_key.get("constraint_name") if isinstance(target_key, dict) else None
        ),
        target_key_kind=(
            _key_kind_public(CatalogKeyKind(target_key["key_kind"]))
            if isinstance(target_key, dict) and target_key.get("key_kind") in {"PRIMARY_KEY", "UNIQUE"}
            else None
        ),
        confirmed_by_user_id=version.confirmed_by_user_id,
        confirmed_at=version.confirmed_at,
        state=_state_for(definition),
        needs_review_reason=definition.needs_review_reason,
        retired_at=definition.retired_at,
        current_catalog=(
            RelationshipCurrentCatalogView(
                snapshot_id=resolved.snapshot_id,
                foreign_key_id=resolved.foreign_key_id,
                base_asset_id=resolved.base_asset_id,
                base_schema_name=resolved.base_schema_name,
                base_table_name=resolved.base_table_name,
                lookup_asset_id=resolved.lookup_asset_id,
                lookup_schema_name=resolved.lookup_schema_name,
                lookup_table_name=resolved.lookup_table_name,
                join_type=resolved.join_type,
                column_pairs=tuple(
                    RelationshipColumnPair(
                        ordinal_position=pair.ordinal_position,
                        base_column_id=pair.base_column_id,
                        base_column_name=pair.base_column_name,
                        lookup_column_id=pair.lookup_column_id,
                        lookup_column_name=pair.lookup_column_name,
                    )
                    for pair in resolved.column_pairs
                ),
                target_key=resolved.target_key,
            )
            if resolved is not None
            else None
        ),
    )


async def create_relationship_definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: RelationshipDefinitionWriteRequest,
) -> RelationshipDefinitionView:
    """Create one logical relationship and immutable confirmed version one."""

    candidate, payload = await _validate_confirmation_request(
        session,
        source_id=source_id,
        owner_user_id=owner_user_id,
        request=request,
    )
    identity = _identity_fingerprint(
        base_asset=candidate.base_asset,
        lookup_asset=candidate.lookup_asset,
        foreign_key=candidate.foreign_key,
    )
    if await _active_identity_conflict(
        session, source_id=source_id, identity_fingerprint=identity
    ):
        raise RelationshipConflictError(
            "an active approval already exists for this declared relationship"
        )
    definition_id = uuid.uuid4()
    version_id = uuid.uuid4()
    definition = RelationshipDefinition(
        id=definition_id,
        source_id=source_id,
        owner_user_id=owner_user_id,
        display_name=_display_name(
            base_asset=candidate.base_asset,
            lookup_asset=candidate.lookup_asset,
            foreign_key=candidate.foreign_key,
        ),
        identity_fingerprint=identity,
        lifecycle_state=RelationshipLifecycleState.ACTIVE,
        current_version_number=1,
    )
    version = RelationshipDefinitionVersion(
        id=version_id,
        source_id=source_id,
        relationship_definition_id=definition_id,
        version_number=1,
        confirmed_snapshot_id=candidate.context.snapshot.id,
        foreign_key_id=candidate.foreign_key.id,
        base_asset_id=candidate.base_asset.id,
        lookup_asset_id=candidate.lookup_asset.id,
        target_key_id=candidate.target_key.id,
        join_type=_join_type_enum(request.join_type),
        definition_json=payload,
        confirmed_by_user_id=owner_user_id,
        confirmed_at=_now(),
    )
    try:
        session.add_all((definition, version))
        await session.commit()
    except IntegrityError:
        await _rollback_quietly(session)
        raise RelationshipConflictError(
            "an active approval already exists for this declared relationship"
        ) from None
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    return _view_from_rows(definition, version)


async def create_relationship_definition_version_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: RelationshipDefinitionWriteRequest,
) -> RelationshipDefinitionView:
    """Append a complete fresh owner confirmation to the same relationship."""

    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=owner_user_id,
        lock=True,
    )
    if definition.lifecycle_state is RelationshipLifecycleState.RETIRED:
        await _rollback_quietly(session)
        raise RelationshipRetiredError()
    candidate, payload = await _validate_confirmation_request(
        session,
        source_id=source_id,
        owner_user_id=owner_user_id,
        request=request,
    )
    identity = _identity_fingerprint(
        base_asset=candidate.base_asset,
        lookup_asset=candidate.lookup_asset,
        foreign_key=candidate.foreign_key,
    )
    if identity != definition.identity_fingerprint:
        await _rollback_quietly(session)
        raise RelationshipValidationError(
            "a relationship version must preserve the approved base-to-lookup direction"
        )
    if await _active_identity_conflict(
        session,
        source_id=source_id,
        identity_fingerprint=identity,
        except_relationship_id=definition.id,
    ):
        await _rollback_quietly(session)
        raise RelationshipConflictError(
            "an active approval already exists for this declared relationship"
        )
    next_version = definition.current_version_number + 1
    version = RelationshipDefinitionVersion(
        id=uuid.uuid4(),
        source_id=source_id,
        relationship_definition_id=definition.id,
        version_number=next_version,
        confirmed_snapshot_id=candidate.context.snapshot.id,
        foreign_key_id=candidate.foreign_key.id,
        base_asset_id=candidate.base_asset.id,
        lookup_asset_id=candidate.lookup_asset.id,
        target_key_id=candidate.target_key.id,
        join_type=_join_type_enum(request.join_type),
        definition_json=payload,
        confirmed_by_user_id=owner_user_id,
        confirmed_at=_now(),
    )
    definition.display_name = _display_name(
        base_asset=candidate.base_asset,
        lookup_asset=candidate.lookup_asset,
        foreign_key=candidate.foreign_key,
    )
    definition.current_version_number = next_version
    definition.lifecycle_state = RelationshipLifecycleState.ACTIVE
    definition.needs_review_at = None
    definition.needs_review_reason = None
    try:
        session.add(version)
        await session.commit()
    except IntegrityError:
        await _rollback_quietly(session)
        raise RelationshipConflictError(
            "an active approval already exists for this declared relationship"
        ) from None
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    return _view_from_rows(definition, version)


async def retire_relationship_definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> RelationshipDefinitionView:
    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=owner_user_id,
        lock=True,
    )
    if definition.lifecycle_state is not RelationshipLifecycleState.RETIRED:
        definition.lifecycle_state = RelationshipLifecycleState.RETIRED
        definition.retired_at = _now()
        definition.retired_by_user_id = owner_user_id
        try:
            await session.commit()
        except SQLAlchemyError:
            await _rollback_quietly(session)
            raise RelationshipPersistenceError() from None
    version = await _version_for_definition(
        session,
        definition=definition,
        version_number=definition.current_version_number,
    )
    return _view_from_rows(definition, version)


async def get_relationship_definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> RelationshipDefinitionView:
    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=owner_user_id,
    )
    version = await _version_for_definition(
        session,
        definition=definition,
        version_number=definition.current_version_number,
    )
    resolved: ResolvedRelationshipDefinition | None = None
    if definition.lifecycle_state is RelationshipLifecycleState.ACTIVE:
        try:
            resolved = await _rematerialize_version(
                session,
                definition=definition,
                version=version,
                owner_user_id=owner_user_id,
            )
        except RelationshipCatalogUnavailableError:
            # Browsing does not convert a transient catalog fault into review.
            pass
        except _RelationshipRequirementError as exc:
            await _mark_needs_review(session, definition=definition, reason=exc.reason)
            definition = await _definition_for_owner(
                session,
                source_id=source_id,
                relationship_id=relationship_id,
                owner_user_id=owner_user_id,
            )
    return _view_from_rows(definition, version, resolved=resolved)


async def list_relationship_definitions_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    include_retired: bool = True,
) -> tuple[RelationshipDefinitionView, ...]:
    # Preserve source existence non-enumerability even where no definitions
    # exist.  ``get_source_for_owner`` is the established owner boundary.
    await get_source_for_owner(session, source_id=source_id, owner_user_id=owner_user_id)
    conditions = [
        RelationshipDefinition.source_id == source_id,
        RelationshipDefinition.owner_user_id == owner_user_id,
    ]
    if not include_retired:
        conditions.append(RelationshipDefinition.lifecycle_state != RelationshipLifecycleState.RETIRED)
    try:
        definitions = (
            await session.execute(
                select(RelationshipDefinition)
                .where(*conditions)
                .order_by(RelationshipDefinition.display_name, RelationshipDefinition.id)
                .limit(_MAX_RELATIONSHIP_DEFINITIONS + 1)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    if len(definitions) > _MAX_RELATIONSHIP_DEFINITIONS:
        raise RelationshipPersistenceError()
    views: list[RelationshipDefinitionView] = []
    for definition in definitions:
        version = await _version_for_definition(
            session,
            definition=definition,
            version_number=definition.current_version_number,
        )
        resolved: ResolvedRelationshipDefinition | None = None
        if definition.lifecycle_state is RelationshipLifecycleState.ACTIVE:
            try:
                resolved = await _rematerialize_version(
                    session,
                    definition=definition,
                    version=version,
                    owner_user_id=owner_user_id,
                )
            except RelationshipCatalogUnavailableError:
                pass
            except _RelationshipRequirementError as exc:
                await _mark_needs_review(session, definition=definition, reason=exc.reason)
        views.append(_view_from_rows(definition, version, resolved=resolved))
    return tuple(views)


async def list_relationship_definition_versions_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> tuple[RelationshipDefinitionView, ...]:
    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=owner_user_id,
    )
    try:
        versions = (
            await session.execute(
                select(RelationshipDefinitionVersion)
                .where(
                    RelationshipDefinitionVersion.source_id == source_id,
                    RelationshipDefinitionVersion.relationship_definition_id == definition.id,
                )
                .order_by(RelationshipDefinitionVersion.version_number.desc())
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    return tuple(_view_from_rows(definition, version) for version in versions)


async def resolve_relationship_definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    snapshot_id: uuid.UUID | None = None,
    relationship_version_id: uuid.UUID | None = None,
    version_number: int | None = None,
    require_current: bool = True,
) -> ResolvedRelationshipDefinition:
    """Return one current, owner-approved relationship or fail closed.

    ``snapshot_id`` and ``relationship_version_id`` are optional planner pins.
    When supplied they must match the current rematerialized relationship;
    neither historical snapshot IDs nor old versions are execution authority.
    """

    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=owner_user_id,
    )
    if definition.lifecycle_state is RelationshipLifecycleState.RETIRED:
        raise RelationshipRetiredError()
    if definition.lifecycle_state is RelationshipLifecycleState.NEEDS_REVIEW:
        raise RelationshipNeedsReviewError()
    selected_version = version_number or definition.current_version_number
    if require_current and selected_version != definition.current_version_number:
        raise RelationshipNeedsReviewError(
            "relationship definition changed; use its current owner-confirmed version"
        )
    version = await _version_for_definition(
        session,
        definition=definition,
        version_number=selected_version,
        relationship_version_id=relationship_version_id,
    )
    try:
        resolved = await _rematerialize_version(
            session,
            definition=definition,
            version=version,
            owner_user_id=owner_user_id,
        )
    except RelationshipCatalogUnavailableError:
        raise
    except _RelationshipRequirementError as exc:
        await _mark_needs_review(session, definition=definition, reason=exc.reason)
        raise RelationshipNeedsReviewError() from None
    if snapshot_id is not None and resolved.snapshot_id != snapshot_id:
        raise RelationshipNeedsReviewError(
            "relationship catalog changed; use the current approved relationship"
        )
    return resolved


async def verify_relationship_pin_for_execution(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    version_number: int,
    snapshot_id: uuid.UUID,
    relationship_version_id: uuid.UUID | None = None,
) -> bool:
    """Recheck a relationship pin around execution without revealing why."""

    try:
        resolved = await resolve_relationship_definition_for_owner(
            session,
            owner_user_id=owner_user_id,
            source_id=source_id,
            relationship_id=relationship_id,
            version_number=version_number,
            snapshot_id=snapshot_id,
            relationship_version_id=relationship_version_id,
            require_current=True,
        )
    except (RelationshipServiceError, NotFoundError):
        return False
    return (
        resolved.snapshot_id == snapshot_id
        and (
            relationship_version_id is None
            or resolved.relationship_version_id == relationship_version_id
        )
    )


async def list_resolved_relationships_for_base_asset(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    base_asset_id: uuid.UUID,
    snapshot_id: uuid.UUID | None = None,
) -> tuple[ResolvedRelationshipDefinition, ...]:
    """Return only current active approvals usable from one current base table."""

    context = await _current_context(
        session, source_id=source_id, owner_user_id=owner_user_id
    )
    if snapshot_id is not None and context.snapshot.id != snapshot_id:
        raise RelationshipNeedsReviewError(
            "relationship catalog changed; use the current selected source snapshot"
        )
    if not any(asset.id == base_asset_id for asset in context.assets):
        raise NotFoundError("catalog asset not found")
    try:
        definitions = (
            await session.execute(
                select(RelationshipDefinition)
                .where(
                    RelationshipDefinition.source_id == source_id,
                    RelationshipDefinition.owner_user_id == owner_user_id,
                    RelationshipDefinition.lifecycle_state == RelationshipLifecycleState.ACTIVE,
                )
                .order_by(RelationshipDefinition.display_name, RelationshipDefinition.id)
                .limit(_MAX_RELATIONSHIP_DEFINITIONS + 1)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    if len(definitions) > _MAX_RELATIONSHIP_DEFINITIONS:
        raise RelationshipPersistenceError()
    resolved_items: list[ResolvedRelationshipDefinition] = []
    for definition in definitions:
        version = await _version_for_definition(
            session,
            definition=definition,
            version_number=definition.current_version_number,
        )
        try:
            resolved = await _rematerialize_version(
                session,
                definition=definition,
                version=version,
                owner_user_id=owner_user_id,
            )
        except RelationshipCatalogUnavailableError:
            raise
        except _RelationshipRequirementError as exc:
            await _mark_needs_review(session, definition=definition, reason=exc.reason)
            continue
        if resolved.base_asset_id == base_asset_id:
            resolved_items.append(resolved)
    return tuple(resolved_items)


async def list_eligible_declared_relationships_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> tuple[EligibleDeclaredRelationship, ...]:
    """List current declared FK directions that can safely be confirmed.

    Relationships whose targets are outside scope, self-referential, lack a
    matching declared unique/primary key, or use unsupported equality types
    are intentionally absent.  This endpoint never guesses a relationship.
    """

    context = await _current_context(
        session, source_id=source_id, owner_user_id=owner_user_id
    )
    asset_ids = tuple(asset.id for asset in context.assets)
    try:
        foreign_keys = (
            await session.execute(
                select(CatalogForeignKey)
                .where(
                    CatalogForeignKey.source_id == source_id,
                    CatalogForeignKey.snapshot_id == context.snapshot.id,
                    CatalogForeignKey.target_status == CatalogForeignKeyTargetStatus.IN_SCOPE,
                    CatalogForeignKey.referencing_asset_id.in_(asset_ids),
                    CatalogForeignKey.referenced_asset_id.in_(asset_ids),
                )
                .order_by(
                    CatalogForeignKey.referencing_asset_id,
                    CatalogForeignKey.constraint_name,
                    CatalogForeignKey.id,
                )
                .limit(_MAX_ELIGIBLE_RELATIONSHIPS + 1)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise RelationshipPersistenceError() from None
    if len(foreign_keys) > _MAX_ELIGIBLE_RELATIONSHIPS:
        raise RelationshipCatalogUnavailableError()
    items: list[EligibleDeclaredRelationship] = []
    for foreign_key in foreign_keys:
        try:
            candidate = await _candidate_for_foreign_key(
                session,
                context=context,
                foreign_key_id=foreign_key.id,
            )
        except _RelationshipRequirementError:
            continue
        items.append(
            EligibleDeclaredRelationship(
                snapshot_id=context.snapshot.id,
                foreign_key_id=candidate.foreign_key.id,
                constraint_name=candidate.foreign_key.constraint_name,
                base_asset_id=candidate.base_asset.id,
                base_schema_name=candidate.base_asset.schema_name,
                base_table_name=candidate.base_asset.table_name,
                lookup_asset_id=candidate.lookup_asset.id,
                lookup_schema_name=candidate.lookup_asset.schema_name,
                lookup_table_name=candidate.lookup_asset.table_name,
                column_pairs=_public_pairs(candidate.mappings),
                target_key=_target_key_view(candidate.target_key, candidate.target_key_columns),
            )
        )
    return tuple(items)


def relationship_provenance(
    resolved: ResolvedRelationshipDefinition,
) -> RelationshipProvenance:
    """Return safe join facts without SQL, bound values, or result rows."""

    return RelationshipProvenance(
        relationship_id=resolved.relationship_id,
        relationship_version_id=resolved.relationship_version_id,
        version_number=resolved.version_number,
        base_table_name=resolved.base_table_name,
        lookup_table_name=resolved.lookup_table_name,
        join_type=resolved.join_type,
        target_key_kind=resolved.target_key.key_kind,
        target_key_constraint_name=resolved.target_key.constraint_name,
        column_pair_count=len(resolved.column_pairs),
    )


__all__ = [
    "EligibleDeclaredRelationship",
    "RelationshipCatalogUnavailableError",
    "RelationshipColumnPair",
    "RelationshipConflictError",
    "RelationshipCurrentCatalogView",
    "RelationshipDefinitionView",
    "RelationshipNeedsReviewError",
    "RelationshipPersistenceError",
    "RelationshipProvenance",
    "RelationshipRetiredError",
    "RelationshipServiceError",
    "RelationshipValidationError",
    "ResolvedRelationshipColumnPair",
    "ResolvedRelationshipDefinition",
    "ResolvedRelationshipTargetKey",
    "create_relationship_definition_for_owner",
    "create_relationship_definition_version_for_owner",
    "get_relationship_definition_for_owner",
    "list_eligible_declared_relationships_for_owner",
    "list_relationship_definition_versions_for_owner",
    "list_relationship_definitions_for_owner",
    "list_resolved_relationships_for_base_asset",
    "relationship_provenance",
    "resolve_relationship_definition_for_owner",
    "retire_relationship_definition_for_owner",
    "verify_relationship_pin_for_execution",
]
