"""Sealed validated query (SPEC-05 B1): the only SQL object the execution adapter accepts.

``ValidatedQuery`` is a plain record, so anything can construct one. ``SealedValidatedQuery`` can only be
minted by :func:`seal`, which re-runs the validator on the text it is handed and refuses unless the text is
byte-identical to what the validator would regenerate. The adapter checks the mint by identity (the same
pattern as the compiler's ``_COMPILED_QUERY_SEAL``), so a hand-built object, a ``dataclasses.replace`` copy,
a subclass, a ``copy`` or a pickle round-trip are all refused before any connection is opened.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import UUID

import sqlglot
from sqlglot import exp

from .types import CatalogScope, Rejection, ValidatedQuery
from .validator import _DIALECT, validate_sql

_SEAL = object()   # module-private; never exported


class SealError(Exception):
    """The validated query does not match the catalog scope it claims (a caller bug, not a model error)."""


@dataclass(frozen=True)
class SealedTable:
    schema: str
    name: str
    asset_id: UUID


@dataclass(frozen=True)
class SealedColumn:
    schema: str
    table: str
    name: str
    column_id: UUID


@dataclass(frozen=True)
class SealedValidatedQuery:
    sql: str
    structural_fingerprint: str
    tables: tuple[SealedTable, ...]            # distinct, canonical (schema, name) order
    columns: tuple[SealedColumn, ...]          # referenced columns, canonical order
    applied_limit: int
    source_id: UUID
    snapshot_id: UUID
    row_cap: int
    # init=False: ``dataclasses.replace`` cannot carry the seal across, so a copy is unsealed.
    _seal: object | None = field(default=None, init=False, repr=False, compare=False)

    def __copy__(self):
        raise TypeError("a sealed query cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("a sealed query cannot be copied")

    def __reduce_ex__(self, protocol):
        raise TypeError("a sealed query cannot be pickled")


def is_sealed(value: object) -> bool:
    return type(value) is SealedValidatedQuery and value._seal is _SEAL


def structural_fingerprint(sql: str) -> str:
    """sha256 of the SQL with every literal replaced by a placeholder (SPEC-05 F10).

    Queries that differ only in literal values hash identically, so the fingerprint can be stored without
    leaking a value. Raises ``sqlglot`` errors if ``sql`` does not parse; callers pass validator output.
    """
    tree = sqlglot.parse_one(sql, dialect=_DIALECT)
    tree = tree.transform(lambda node: exp.Placeholder() if isinstance(node, exp.Literal) else node)
    return hashlib.sha256(tree.sql(dialect=_DIALECT, comments=False).encode("utf-8")).hexdigest()


def seal(validated: ValidatedQuery, scope: CatalogScope, *, row_cap: int) -> SealedValidatedQuery:
    """Mint a sealed query, or raise :class:`SealError`.

    The validator is re-run on ``validated.sql`` against ``scope``: the result must be the same text with
    the same tables, columns and limit. That makes a forged or stale ``ValidatedQuery`` unsealable.
    """
    if not isinstance(validated, ValidatedQuery):
        raise SealError("not a validated query")
    if validated.source_id != scope.source_id or validated.snapshot_id != scope.snapshot_id:
        raise SealError("scope mismatch")
    again = validate_sql(validated.sql, scope, row_cap=row_cap)
    if (
        isinstance(again, Rejection)
        or again.sql != validated.sql
        or again.tables != validated.tables
        or again.columns != validated.columns
        or again.applied_limit != validated.applied_limit
    ):
        raise SealError("validated query does not match the scope")

    by_name = {(t.schema, t.name): t for t in scope.tables}
    tables: list[SealedTable] = []
    for schema, name in sorted(validated.tables):
        table = by_name.get((schema, name))
        if table is None or table.asset_id is None:
            raise SealError("table has no catalog asset id")
        tables.append(SealedTable(schema, name, table.asset_id))
    columns: list[SealedColumn] = []
    for schema, table_name, column_name in sorted(validated.columns):
        table = by_name.get((schema, table_name))
        column = next((c for c in table.columns if c.name == column_name), None) if table else None
        if column is None or column.column_id is None:
            raise SealError("column has no catalog column id")
        columns.append(SealedColumn(schema, table_name, column_name, column.column_id))

    sealed = SealedValidatedQuery(
        sql=validated.sql,
        structural_fingerprint=structural_fingerprint(validated.sql),
        tables=tuple(tables),
        columns=tuple(columns),
        applied_limit=validated.applied_limit,
        source_id=validated.source_id,
        snapshot_id=validated.snapshot_id,
        row_cap=row_cap,
    )
    object.__setattr__(sealed, "_seal", _SEAL)
    return sealed
