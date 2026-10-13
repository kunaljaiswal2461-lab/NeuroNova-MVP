"""Public types for the SQL validator (SPEC-04 §6)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from app.core.config import Settings


class RejectionCode(str, Enum):
    EMPTY_INPUT = "empty_input"
    INPUT_TOO_LARGE = "input_too_large"
    INVALID_TEXT = "invalid_text"
    PARSE_ERROR = "parse_error"
    MULTIPLE_STATEMENTS = "multiple_statements"
    UNSUPPORTED_STATEMENT = "unsupported_statement"
    UNSUPPORTED_SYNTAX = "unsupported_syntax"
    TOO_COMPLEX = "too_complex"
    TABLE_NOT_IN_SCOPE = "table_not_in_scope"
    NO_TABLE_REFERENCE = "no_table_reference"
    AMBIGUOUS_TABLE = "ambiguous_table"
    DATABASE_QUALIFIED_TABLE = "database_qualified_table"
    COLUMN_NOT_FOUND = "column_not_found"
    AMBIGUOUS_COLUMN = "ambiguous_column"
    FUNCTION_NOT_ALLOWED = "function_not_allowed"
    SCHEMA_QUALIFIED_FUNCTION = "schema_qualified_function"
    CAST_TYPE_NOT_ALLOWED = "cast_type_not_allowed"
    TABLE_FUNCTION_NOT_ALLOWED = "table_function_not_allowed"
    NON_LITERAL_LIMIT = "non_literal_limit"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ScopeColumn:
    name: str            # exact catalog name (case preserved)
    native_type: str     # catalog native_type_name, e.g. "int8", "timestamptz"
    column_id: UUID | None = None   # CatalogColumn.id; set by load_catalog_scope, needed to seal (SPEC-05)


@dataclass(frozen=True)
class ScopeTable:
    schema: str          # exact catalog schema name
    name: str            # exact catalog table name
    columns: tuple[ScopeColumn, ...]
    asset_id: UUID | None = None    # CatalogAsset.id; set by load_catalog_scope, needed to seal (SPEC-05)


@dataclass(frozen=True)
class CatalogScope:
    source_id: UUID
    snapshot_id: UUID
    tables: tuple[ScopeTable, ...]

    def find(self, schema: str | None, name: str) -> list[ScopeTable]:
        """Exact-name matches; ``schema=None`` matches the name in every schema."""
        return [
            t for t in self.tables
            if t.name == name and (schema is None or t.schema == schema)
        ]


@dataclass(frozen=True)
class GuardLimits:
    max_chars: int = 20_000
    max_paren_depth: int = 32
    max_ast_nodes: int = 3_000
    max_table_refs: int = 30

    @classmethod
    def from_settings(cls, settings: Settings) -> "GuardLimits":
        return cls(
            max_chars=settings.sql_guard_max_chars,
            max_paren_depth=settings.sql_guard_max_paren_depth,
            max_ast_nodes=settings.sql_guard_max_ast_nodes,
        )


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str                                        # regenerated; the only text Part 2 may execute
    tables: frozenset[tuple[str, str]]              # (schema, table) catalog references
    columns: frozenset[tuple[str, str, str]]        # (schema, table, column) catalog references
    functions: frozenset[str]                       # Postgres function names used, lowercase
    model_limit: int | None                         # LIMIT/FETCH the model wrote, None if none
    applied_limit: int                              # LIMIT in sql
    fingerprint: str                                # sha256 hex of sql
    source_id: UUID
    snapshot_id: UUID


@dataclass(frozen=True)
class Rejection:
    code: RejectionCode
    message: str
    identifier: str | None = None
    suggestions: tuple[str, ...] = ()
