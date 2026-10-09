"""Rejection construction and repair-friendly messages (SPEC-04 §10, INV-V7).

A message may contain only: fixed wording, catalog names taken from the scope,
and the offending identifier truncated to 64 characters. Never the raw SQL and
never database error text. ``;`` and ``$`` are removed from echoed identifiers
so a message can never carry a second statement or a dollar-quoted body.
"""
from __future__ import annotations

import difflib
import re

from .types import CatalogScope, Rejection, RejectionCode

MAX_IDENTIFIER_CHARS = 64
MAX_SUGGESTIONS = 5
SUGGESTION_CUTOFF = 0.6

_UNSAFE = re.compile(r"[;$\x00-\x1f\x7f]")


def safe_identifier(value: object | None) -> str | None:
    if value is None:
        return None
    return _UNSAFE.sub("", str(value))[:MAX_IDENTIFIER_CHARS]


def suggest(target: str, pairs: list[tuple[str, str]]) -> tuple[str, ...]:
    """Closest catalog names first. ``pairs`` is ``(label, key)``: ``key`` is compared, ``label`` is returned.

    Deterministic (stdlib difflib, cutoff 0.6, at most 5): ties are broken by label.
    """
    scored: dict[str, float] = {}
    needle = target.lower()
    for label, key in pairs:
        ratio = difflib.SequenceMatcher(None, needle, key.lower()).ratio()
        if ratio >= SUGGESTION_CUTOFF and ratio > scored.get(label, -1.0):
            scored[label] = ratio
    ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    return tuple(label for label, _ in ranked[:MAX_SUGGESTIONS])


def table_pairs(scope: CatalogScope) -> list[tuple[str, str]]:
    return [(f"{t.schema}.{t.name}", t.name) for t in scope.tables]


_FIXED: dict[RejectionCode, str] = {
    RejectionCode.EMPTY_INPUT: "No SQL was produced.",
    RejectionCode.INVALID_TEXT: "The query contains a NUL character.",
    RejectionCode.PARSE_ERROR: "The SQL could not be parsed as PostgreSQL.",
    RejectionCode.MULTIPLE_STATEMENTS: "Exactly one SELECT statement is allowed.",
    RejectionCode.UNSUPPORTED_STATEMENT: "Only SELECT queries are allowed.",
    RejectionCode.NO_TABLE_REFERENCE: "The query must read from at least one available table.",
    RejectionCode.TOO_COMPLEX: "The query is too deeply nested or too large.",
    RejectionCode.SCHEMA_QUALIFIED_FUNCTION: "Call functions without a schema prefix.",
    RejectionCode.TABLE_FUNCTION_NOT_ALLOWED: "Only generate_series is allowed in FROM.",
    RejectionCode.NON_LITERAL_LIMIT: "LIMIT and OFFSET must be plain numbers.",
    RejectionCode.INTERNAL_ERROR: "The query could not be validated.",
}


def reject(code: RejectionCode, *, identifier: object | None = None, detail: str | None = None,
           suggestions: tuple[str, ...] = (), max_chars: int | None = None) -> Rejection:
    """Build a Rejection. ``detail`` must be fixed wording chosen by the validator."""
    ident = safe_identifier(identifier)
    if code is RejectionCode.INPUT_TOO_LARGE:
        message = f"The query is too long (limit {max_chars:,} characters)." if max_chars else "The query is too long."
    elif code is RejectionCode.UNSUPPORTED_SYNTAX:
        message = detail or "This SQL construct is not supported."
    elif code is RejectionCode.TABLE_NOT_IN_SCOPE:
        message = f"Table '{ident}' is not available."
        if suggestions:
            message += " Available tables include: " + ", ".join(suggestions) + "."
    elif code is RejectionCode.AMBIGUOUS_TABLE:
        message = f"Table name '{ident}' exists in more than one schema. Qualify it with a schema name."
    elif code is RejectionCode.DATABASE_QUALIFIED_TABLE:
        message = f"Remove the database name from '{ident}'."
    elif code is RejectionCode.COLUMN_NOT_FOUND:
        message = f"Column '{ident}' does not exist in the referenced tables."
        if suggestions:
            message += " Similar: " + ", ".join(suggestions) + "."
    elif code is RejectionCode.AMBIGUOUS_COLUMN:
        message = f"Column '{ident}' is ambiguous. Qualify it with a table alias."
    elif code is RejectionCode.FUNCTION_NOT_ALLOWED:
        message = detail or f"Function '{ident}' is not allowed."
    elif code is RejectionCode.CAST_TYPE_NOT_ALLOWED:
        message = f"Casting to '{ident}' is not allowed."
    else:
        message = _FIXED[code]
    return Rejection(code=code, message=message, identifier=ident, suggestions=suggestions)
