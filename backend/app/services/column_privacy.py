"""Personal-data flags per text column and the per-source AI settings (SPEC-09 W3.1, W3.2).

The *suggestion* is computed from the column and table names and the type; it is never stored. The owner's override
(``source_column_privacy``) wins when present. Both are keyed by name so they survive re-syncs. Values never pass
through this module.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.catalog import CatalogAsset, CatalogColumn, SourceCatalogHead
from app.db.models.source import SourceColumnPrivacy
from app.exceptions.custom_exceptions import AppError
from app.services.sample_values.pii import column_name_is_pii
from app.services.sample_values.store import _owned_source, _rollback

TEXT_NATIVE_TYPES = frozenset({"text", "varchar", "bpchar"})
FREE_TEXT_HINTS = ("comment", "note", "description", "reason", "message", "address")
MAX_CHANGES = 2000
STATES = ("personal", "not_personal")

ColumnKey = tuple[str, str, str]


class ColumnPrivacyError(AppError):
    pass


class ColumnPrivacyInputError(ColumnPrivacyError):
    status_code = 422
    code = "invalid_column_privacy_request"

    def __init__(self, message: str = "column privacy request is invalid"):
        super().__init__(message)


class ColumnPrivacyPersistenceError(ColumnPrivacyError):
    status_code = 503
    code = "column_privacy_persistence_failed"

    def __init__(self):
        super().__init__("could not complete the column privacy request")


def is_text_type(native_type: str | None) -> bool:
    return (native_type or "").lower() in TEXT_NATIVE_TYPES


def suggested_personal(table: str, column: str, native_type: str | None) -> bool:
    """Part 4's name rules, plus: a text column whose name says free text (comment, note, reason ...)."""
    if column_name_is_pii(table, column):
        return True
    return is_text_type(native_type) and any(hint in column.lower() for hint in FREE_TEXT_HINTS)


def effective_personal(key: ColumnKey, native_type: str | None, overrides: dict[ColumnKey, str]) -> bool:
    state = overrides.get(key)
    if state is not None:
        return state == "personal"
    return suggested_personal(key[1], key[2], native_type)


async def load_overrides(session: AsyncSession, source_id: uuid.UUID) -> dict[ColumnKey, str]:
    rows = (
        await session.execute(
            select(
                SourceColumnPrivacy.schema_name, SourceColumnPrivacy.table_name, SourceColumnPrivacy.column_name,
                SourceColumnPrivacy.state,
            ).where(SourceColumnPrivacy.source_id == source_id)
        )
    ).all()
    return {(r[0], r[1], r[2]): r[3] for r in rows}


@dataclass(frozen=True)
class ColumnPrivacyRow:
    schema: str
    table: str
    column: str
    type: str
    suggested_personal: bool
    state: str | None
    effective_personal: bool


async def _head_text_columns(session: AsyncSession, source_id: uuid.UUID) -> list[tuple[str, str, str, str]]:
    head = await session.get(SourceCatalogHead, source_id)
    if head is None:
        return []
    rows = (
        await session.execute(
            select(
                CatalogAsset.schema_name, CatalogAsset.table_name, CatalogColumn.column_name,
                CatalogColumn.native_type_name,
            )
            .join(CatalogAsset, (CatalogAsset.source_id == CatalogColumn.source_id)
                  & (CatalogAsset.snapshot_id == CatalogColumn.snapshot_id)
                  & (CatalogAsset.id == CatalogColumn.asset_id))
            .where(CatalogColumn.source_id == source_id, CatalogColumn.snapshot_id == head.snapshot_id)
        )
    ).all()
    return sorted((r[0], r[1], r[2], r[3] or "") for r in rows if is_text_type(r[3]))


async def list_column_privacy(
    session: AsyncSession, *, source_id: uuid.UUID, owner_user_id: uuid.UUID
) -> list[ColumnPrivacyRow]:
    """The head snapshot's text columns with suggestion, override and effective flag. No values."""
    try:
        await _owned_source(session, source_id, owner_user_id, lock=False)
        columns = await _head_text_columns(session, source_id)
        overrides = await load_overrides(session, source_id)
        await session.rollback()
    except SQLAlchemyError:
        await _rollback(session)
        raise ColumnPrivacyPersistenceError() from None
    return [
        ColumnPrivacyRow(
            schema=s, table=t, column=c, type=ty,
            suggested_personal=suggested_personal(t, c, ty),
            state=overrides.get((s, t, c)),
            effective_personal=effective_personal((s, t, c), ty, overrides),
        )
        for s, t, c, ty in columns
    ]


async def update_column_privacy(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    changes: Sequence[tuple[ColumnKey, str | None]],
) -> None:
    """Set or (``None``) clear overrides. Every column must be a text column of the head snapshot."""
    if len(changes) > MAX_CHANGES:
        raise ColumnPrivacyInputError("too many changes")
    for key, state in changes:
        if len(key) != 3 or any(not isinstance(p, str) or not p or len(p) > 63 or "\x00" in p for p in key):
            raise ColumnPrivacyInputError("a column needs a schema, table and column name")
        if state is not None and state not in STATES:
            raise ColumnPrivacyInputError("state must be personal, not_personal or null")
    try:
        await _owned_source(session, source_id, owner_user_id, lock=True)
        known = {(s, t, c) for s, t, c, _ in await _head_text_columns(session, source_id)}
        if any(key not in known for key, _ in changes):
            await _rollback(session)
            raise ColumnPrivacyInputError("unknown text column")
        for (schema, table, column), state in changes:
            await session.execute(
                delete(SourceColumnPrivacy).where(
                    SourceColumnPrivacy.source_id == source_id,
                    SourceColumnPrivacy.schema_name == schema,
                    SourceColumnPrivacy.table_name == table,
                    SourceColumnPrivacy.column_name == column,
                )
            )
            if state is not None:
                session.add(
                    SourceColumnPrivacy(
                        source_id=source_id, schema_name=schema, table_name=table, column_name=column,
                        state=state, updated_by_user_id=owner_user_id,
                    )
                )
        await session.commit()
    except SQLAlchemyError:
        await _rollback(session)
        raise ColumnPrivacyPersistenceError() from None


# ── AI settings (the result-rows consent) ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AiSettings:
    result_rows_to_model: bool
    sample_values_enabled: bool
    result_rows_to_model_generation: int


async def get_ai_settings(session: AsyncSession, *, source_id: uuid.UUID, owner_user_id: uuid.UUID) -> AiSettings:
    try:
        source = await _owned_source(session, source_id, owner_user_id, lock=False)
        settings = AiSettings(
            bool(source.result_rows_to_model_enabled), bool(source.sample_values_enabled),
            int(source.result_rows_to_model_generation),
        )
        await session.rollback()
        return settings
    except SQLAlchemyError:
        await _rollback(session)
        raise ColumnPrivacyPersistenceError() from None


async def update_ai_settings(
    session: AsyncSession, *, source_id: uuid.UUID, owner_user_id: uuid.UUID, result_rows_to_model: bool | None
) -> AiSettings:
    if result_rows_to_model is not None and not isinstance(result_rows_to_model, bool):
        raise ColumnPrivacyInputError()
    try:
        source = await _owned_source(session, source_id, owner_user_id, lock=True)
        if result_rows_to_model is not None and source.result_rows_to_model_enabled != result_rows_to_model:
            source.result_rows_to_model_enabled = result_rows_to_model
            source.result_rows_to_model_generation += 1
        settings = AiSettings(
            bool(source.result_rows_to_model_enabled), bool(source.sample_values_enabled),
            int(source.result_rows_to_model_generation),
        )
        await session.commit()
        return settings
    except SQLAlchemyError:
        await _rollback(session)
        raise ColumnPrivacyPersistenceError() from None


async def read_consent(session_factory, *, source_id: uuid.UUID, owner_user_id: uuid.UUID) -> tuple[bool, int]:
    """Fresh read of the consent flag and its generation. Fails closed: any problem means *no consent*."""
    try:
        async with session_factory() as session:
            try:
                source = await _owned_source(session, source_id, owner_user_id, lock=False)
                return bool(source.result_rows_to_model_enabled), int(source.result_rows_to_model_generation)
            finally:
                await session.rollback()
    except Exception:       # noqa: BLE001 - fail closed
        return False, 0


async def load_effective_flags(session: AsyncSession, source_id: uuid.UUID) -> "PrivacyFlags":
    """Effective personal flags for every text column of the head snapshot (read once per turn)."""
    columns = await _head_text_columns(session, source_id)
    overrides = await load_overrides(session, source_id)
    return PrivacyFlags(
        {(s, t, c): effective_personal((s, t, c), ty, overrides) for s, t, c, ty in columns}, overrides
    )


@dataclass(frozen=True)
class PrivacyFlags:
    """``personal[(schema, table, column)]`` for text columns; non-text columns are not in it (they are not personal
    unless the owner said so). Pure data, safe to pass around."""

    personal: dict[ColumnKey, bool]
    overrides: dict[ColumnKey, str]

    def is_personal(self, schema: str, table: str, column: str) -> bool:
        key = (schema, table, column)
        if key in self.personal:
            return self.personal[key]
        state = self.overrides.get(key)
        if state is not None:
            return state == "personal"
        return column_name_is_pii(table, column)
