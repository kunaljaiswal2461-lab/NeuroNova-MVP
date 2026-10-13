"""Outcome classification and the repair brief (SPEC-09 W1, C5).

The brief is built only from text this project wrote: fixed problem sentences, the validator's own messages, the
model's previous SQL and catalog suggestions. It never carries Postgres server text (DETAIL, CONTEXT, HINT), a
SQLSTATE's message, or a row value (I11).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.schema_context.literals import untrusted_literal
from app.services.sql_execution import SqlExecutionResult
from app.services.sql_guard import Rejection, RejectionCode

MAX_PREVIOUS_SQL_CHARS = 4_000
MAX_SUGGESTIONS = 5


class Outcome(str, Enum):
    REPAIRABLE = "repairable"
    REGENERATE = "regenerate"
    FINAL = "final"


@dataclass(frozen=True)
class Classification:
    outcome: Outcome
    code: str                      # the user-message code (C6): a rejection code or a failure code
    reason_code: str               # what is recorded as the repair reason (``query_error:42803`` for a SQLSTATE)
    problem: str | None = None     # fixed sentence for the brief
    identifier: str | None = None
    suggestions: tuple[str, ...] = ()


_REPAIRABLE_REJECTIONS = frozenset({
    RejectionCode.PARSE_ERROR, RejectionCode.UNSUPPORTED_SYNTAX, RejectionCode.MULTIPLE_STATEMENTS,
    RejectionCode.UNSUPPORTED_STATEMENT, RejectionCode.TOO_COMPLEX, RejectionCode.NO_TABLE_REFERENCE,
    RejectionCode.TABLE_NOT_IN_SCOPE, RejectionCode.AMBIGUOUS_TABLE, RejectionCode.DATABASE_QUALIFIED_TABLE,
    RejectionCode.COLUMN_NOT_FOUND, RejectionCode.AMBIGUOUS_COLUMN, RejectionCode.FUNCTION_NOT_ALLOWED,
    RejectionCode.SCHEMA_QUALIFIED_FUNCTION, RejectionCode.CAST_TYPE_NOT_ALLOWED,
    RejectionCode.TABLE_FUNCTION_NOT_ALLOWED, RejectionCode.NON_LITERAL_LIMIT,
})

SQLSTATE_PROBLEMS: dict[str, str] = {
    "42601": "Syntax error.",
    "42703": "A column, table or function does not exist or does not take these argument types.",
    "42P01": "A column, table or function does not exist or does not take these argument types.",
    "42883": "A column, table or function does not exist or does not take these argument types.",
    "42702": "A column reference is ambiguous; qualify it with its table.",
    "42804": "A value has the wrong type; add an explicit cast or compare like with like.",
    "22P02": "A value has the wrong type; add an explicit cast or compare like with like.",
    "42803": "Grouping error: every selected non-aggregate column must be in GROUP BY.",
    "22012": "Division by zero: guard the divisor with NULLIF(x, 0).",
    "22003": "A numeric or date value is out of range.",
    "22008": "A numeric or date value is out of range.",
}
_EXPENSIVE = "The query is too expensive. Filter earlier, avoid cross joins, aggregate before joining large tables."
_ROW_TOO_LARGE = "A result row is too large. Select fewer or shorter columns."
ENVELOPE_PROBLEM = "The output did not match the required format ({sub}).".format
ENVELOPE_SUB_CODES = frozenset({"length", "refusal", "invalid_json", "not_valid"})


def classify_pass1_failure(code: str, detail: str | None = None) -> Classification:
    if code == "envelope_invalid":
        sub = detail if detail in ENVELOPE_SUB_CODES else "not_valid"
        return Classification(Outcome.REPAIRABLE, code, f"envelope_invalid:{sub}", ENVELOPE_PROBLEM(sub=sub))
    return Classification(Outcome.FINAL, code, code)


def classify_rejection(rejection: Rejection) -> Classification:
    code = rejection.code
    if code in _REPAIRABLE_REJECTIONS:
        return Classification(
            Outcome.REPAIRABLE, code.value, code.value, rejection.message, rejection.identifier,
            tuple(rejection.suggestions)[:MAX_SUGGESTIONS],
        )
    return Classification(Outcome.FINAL, code.value, code.value)


def classify_execution(result: SqlExecutionResult) -> Classification:
    """``result`` is a non-success. (A success is classified by the caller.)"""
    code = result.failure_code or "execution_failed"
    if code == "schema_changed":
        return Classification(Outcome.REGENERATE, code, code)
    if code == "query_error":
        problem = SQLSTATE_PROBLEMS.get(result.error_class or "")
        if problem is not None:
            return Classification(Outcome.REPAIRABLE, code, f"query_error:{result.error_class}", problem)
        return Classification(Outcome.FINAL, code, code)
    if code == "query_too_expensive":
        return Classification(Outcome.REPAIRABLE, code, code, _EXPENSIVE)
    if code == "unsupported_output_type":
        return Classification(Outcome.REPAIRABLE, code, code, result.message or "An output column has a type that cannot be returned.")
    if code == "result_row_too_large":
        return Classification(Outcome.REPAIRABLE, code, code, _ROW_TOO_LARGE)
    return Classification(Outcome.FINAL, code, code)


def _literal_list(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(untrusted_literal(i, limit=100) for i in items) + "]"


def render_brief(c: Classification, previous_sql: str | None) -> str:
    """The text appended to the user message of the one repair call."""
    lines = ["Your previous answer could not be used."]
    if c.code == "envelope_invalid":
        sub = c.reason_code.split(":", 1)[1] if ":" in c.reason_code else "not_valid"
        lines.append(f"Your previous output was not valid for the required format ({sub}).")
    else:
        if previous_sql is not None:
            lines.append(f"previous_sql: {untrusted_literal(previous_sql, limit=MAX_PREVIOUS_SQL_CHARS)}")
        lines.append(f"problem: {c.code} — {c.problem}")
        if c.identifier:
            lines.append(f"identifier: {untrusted_literal(c.identifier, limit=64)}")
        if c.suggestions:
            lines.append(f"did_you_mean: {_literal_list(c.suggestions[:MAX_SUGGESTIONS])}")
    lines.append("Write a corrected answer for the same question.")
    return "\n".join(lines)
