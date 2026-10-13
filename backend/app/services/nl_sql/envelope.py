"""The strict Pass-1 envelope: JSON schema for the model, Pydantic for the per-kind rules (SPEC-08 B5)."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .leak import any_instruction_leak
from .prompt import CANARY, instruction_windows
from .types import OUTPUT_TYPES, REASON_CODES, Envelope

MAX_SQL_CHARS = 20_000
MAX_ASSUMPTIONS = 8
MAX_ASSUMPTION_CHARS = 200
MAX_TABLES_USED = 30
MAX_CLARIFICATION_CHARS = 300
MAX_REASON_CHARS = 300

_NULLABLE_STRING = {"type": ["string", "null"]}

# Flat on purpose: strict mode wants an object root and every key required. Nullable fields carry the per-kind data.
ENVELOPE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind", "sql", "assumptions", "tables_used", "output_intent", "clarification", "reason_code", "reason",
    ],
    "properties": {
        "kind": {"type": "string", "enum": ["sql", "clarification", "unsupported"]},
        "sql": _NULLABLE_STRING,
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "tables_used": {"type": "array", "items": {"type": "string"}},
        "output_intent": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "encodings"],
                    "properties": {
                        "type": {"type": "string", "enum": list(OUTPUT_TYPES)},
                        "encodings": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["x", "y", "series", "label", "value"],
                            "properties": {
                                "x": _NULLABLE_STRING,
                                "y": {"type": "array", "items": {"type": "string"}},
                                "series": _NULLABLE_STRING,
                                "label": _NULLABLE_STRING,
                                "value": _NULLABLE_STRING,
                            },
                        },
                    },
                },
            ]
        },
        "clarification": _NULLABLE_STRING,
        "reason_code": {"anyOf": [{"type": "null"}, {"type": "string", "enum": list(REASON_CODES)}]},
        "reason": _NULLABLE_STRING,
    },
}

_FENCE = re.compile(r"^\s*```[A-Za-z]*[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```\s*$", re.DOTALL)


class EnvelopeError(Exception):
    """``code`` is ``envelope_invalid``, ``canary_leak`` or ``instruction_leak``. Carries no model content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def strip_fence(sql: str) -> tuple[str, bool]:
    """Remove one surrounding pair of Markdown code fences (``` or ```sql)."""
    match = _FENCE.match(sql)
    if match is None:
        return sql, False
    return match.group(1), True


def parse_envelope(raw: Any) -> tuple[Envelope, tuple[str, ...]]:
    """Check the model's JSON. Returns the envelope and the rewrites applied (``stripped_fence``)."""
    if not isinstance(raw, dict):
        raise EnvelopeError("envelope_invalid")
    # The canary is judged on the raw text first: a leak is never shown, whatever else is wrong with it.
    if any(CANARY in s for s in _strings(raw)):
        raise EnvelopeError("canary_leak")
    if any_instruction_leak(_strings(raw), instruction_windows()):
        raise EnvelopeError("instruction_leak")
    rewrites: list[str] = []
    if isinstance(raw.get("sql"), str):
        stripped, changed = strip_fence(raw["sql"])
        if changed:
            raw = {**raw, "sql": stripped}
            rewrites.append("stripped_fence")
    try:
        env = Envelope.model_validate(raw, strict=True)
    except ValidationError:
        raise EnvelopeError("envelope_invalid") from None
    _check_kind_rules(env)
    return env, tuple(rewrites)


def _non_empty(value: str | None) -> bool:
    return value is not None and value.strip() != ""


def _check_kind_rules(env: Envelope) -> None:
    ok = (
        len(env.assumptions) <= MAX_ASSUMPTIONS
        and all(len(a) <= MAX_ASSUMPTION_CHARS for a in env.assumptions)
        and len(env.tables_used) <= MAX_TABLES_USED
    )
    if env.kind == "sql":
        ok = (
            ok
            and _non_empty(env.sql) and len(env.sql) <= MAX_SQL_CHARS
            and env.output_intent is not None
            and env.clarification is None and env.reason_code is None and env.reason is None
        )
    elif env.kind == "clarification":
        ok = (
            ok
            and _non_empty(env.clarification) and len(env.clarification) <= MAX_CLARIFICATION_CHARS
            and env.sql is None and env.output_intent is None
            and env.reason_code is None and env.reason is None
        )
    else:
        ok = (
            ok
            and env.reason_code is not None
            and (env.reason is None or len(env.reason) <= MAX_REASON_CHARS)
            and env.sql is None and env.output_intent is None and env.clarification is None
        )
    if not ok:
        raise EnvelopeError("envelope_invalid")


def dumps_for_hash(schema: dict) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
