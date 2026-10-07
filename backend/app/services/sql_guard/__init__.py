"""NL->SQL validator (SPEC-04). The executable text is ``ValidatedQuery.sql``, never the model's."""
from .allowlist import ALLOWED_FUNCTIONS
from .types import (
    CatalogScope,
    GuardLimits,
    Rejection,
    RejectionCode,
    ScopeColumn,
    ScopeTable,
    ValidatedQuery,
)
from .scope import load_catalog_scope
from .validator import validate_sql

__all__ = [
    "ALLOWED_FUNCTIONS",
    "CatalogScope",
    "GuardLimits",
    "Rejection",
    "RejectionCode",
    "ScopeColumn",
    "ScopeTable",
    "ValidatedQuery",
    "load_catalog_scope",
    "validate_sql",
]
