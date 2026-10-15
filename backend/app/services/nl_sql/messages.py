"""Every user-visible string of the NL->SQL pipeline (SPEC-09 W8, C6). Fixed text with catalog-name slots only.

No model text and no server text is ever shown as an error. The slots (identifier, suggestions, table names) are
filled by the caller from the catalog: a name that is not in the catalog never reaches a message.
"""
from __future__ import annotations

from collections.abc import Sequence

WRITE_REFUSAL = "This assistant only reads data. It cannot change data or the database structure."
MODEL_NOT_CONFIGURED = "The assistant model is not configured."
CANCELLED = "The database calculation was cancelled. No result rows were used."
ACCESS_CHANGED = "Access to the data changed. The query was not run."
ACCESS_REVOKED = "Access to the data changed while the query ran. No rows are shown."

GENERIC = "The question could not be answered. Please try rephrasing it."
REPHRASE = "I couldn't write a working query for this question. Try rephrasing it, for example by naming the table or columns."
MODEL_UNAVAILABLE = "The assistant model is unavailable right now. Please try again shortly."
UNSAFE = "I couldn't answer that safely. Please rephrase the question."
TOO_EXPENSIVE = "That question needs a very large calculation. Add a time range or a filter and ask again."
TIMEOUT = "The calculation took too long. Add a time range or a filter and ask again."
ROW_TOO_LARGE = "The result rows are too large to show. Ask for fewer or shorter columns."
QUESTION_TOO_LONG = "The question is too long. Please shorten it to under 4,000 characters."
OVER_BUDGET = "This source has too many columns to answer in one question. Select fewer tables for this source."

# Codes whose message is the "rephrase" text: a repairable failure that stayed failed after the one repair.
REPHRASE_CODES = frozenset({
    "query_error", "envelope_invalid", "parse_error", "unsupported_syntax", "multiple_statements",
    "unsupported_statement", "too_complex", "no_table_reference", "ambiguous_table", "database_qualified_table",
    "ambiguous_column", "function_not_allowed", "schema_qualified_function", "cast_type_not_allowed",
    "table_function_not_allowed", "non_literal_limit", "unsupported_output_type",
})
FIXED_BY_CODE = {
    "query_too_expensive": TOO_EXPENSIVE,
    "execution_timeout": TIMEOUT,
    "result_row_too_large": ROW_TOO_LARGE,
    "llm_unavailable": MODEL_UNAVAILABLE,
    "model_unavailable": MODEL_UNAVAILABLE,
    "canary_leak": UNSAFE,
    "instruction_leak": UNSAFE,
    "question_too_long": QUESTION_TOO_LONG,
    "over_budget": OVER_BUDGET,
    "access_changed": ACCESS_CHANGED,
    "access_revoked": ACCESS_REVOKED,
    "execution_cancelled": CANCELLED,
}
ALL_CODES = frozenset({"column_not_found", "table_not_in_scope"}) | REPHRASE_CODES | frozenset(FIXED_BY_CODE)


def _names(items: Sequence[str], limit: int = 5) -> str:
    return ", ".join(items[:limit])


def final_failure_message(
    code: str,
    *,
    identifier: str | None = None,
    columns: Sequence[str] = (),
    tables: Sequence[str] = (),
) -> str:
    """The C6 text for ``code``.

    ``identifier``, ``columns`` and ``tables`` must already be catalog names (the caller checks). An ``identifier``
    that is not a catalog name has to be passed as ``None``: the message then drops that slot instead of echoing
    model text.
    """
    if code in FIXED_BY_CODE:
        return FIXED_BY_CODE[code]
    if code == "column_not_found":
        what = f"a column matching '{identifier}'" if identifier else "a matching column"
        if columns:
            return f"I couldn't find {what}. Columns that exist include: {_names(columns)}. Try rephrasing with one of these."
        return f"I couldn't find {what}. Try rephrasing the question."
    if code == "table_not_in_scope":
        if tables:
            return f"That data isn't in the tables this source can read: {_names(tables)}."
        return "That data isn't in the tables this source can read."
    if code in REPHRASE_CODES:
        return REPHRASE
    return GENERIC
