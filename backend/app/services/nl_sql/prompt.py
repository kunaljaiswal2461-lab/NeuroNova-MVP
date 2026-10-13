"""Prompt assembly for pass 1 (SPEC-08 B3). Pure functions; no I/O.

The system message is the static instructions followed by the schema text, so it is byte-identical for every question
on one snapshot (OpenAI prompt caching works on identical prefixes). Everything per request goes in the user message,
and every piece of text that came from a person or the database is written as one JSON string literal on one line.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.schema_context.literals import untrusted_literal

from .types import Turn

PROMPT_VERSION = "pass1-v2"
CANARY = "NN-PASS1-CANARY-7c3e91ab"

MAX_HISTORY_TURNS = 3
HISTORY_QUESTION_CHARS = 500
HISTORY_SQL_CHARS = 2_000
HISTORY_ASSUMPTIONS = 8
HISTORY_ASSUMPTION_CHARS = 200

_INSTRUCTIONS_PATH = Path(__file__).with_name("prompts") / "pass1_system.md"


@lru_cache(maxsize=1)
def instructions() -> str:
    return _INSTRUCTIONS_PATH.read_text(encoding="utf-8").replace("{{CANARY}}", CANARY).rstrip("\n")


PASS2_PROMPT_VERSION = "pass2-v1"
PASS2_CANARY = "NN-PASS2-CANARY-5d2a84fc"
_PASS2_PATH = Path(__file__).with_name("prompts") / "pass2_system.md"


@lru_cache(maxsize=1)
def pass2_instructions() -> str:
    return _PASS2_PATH.read_text(encoding="utf-8").replace("{{CANARY}}", PASS2_CANARY).rstrip("\n")


@lru_cache(maxsize=1)
def pass2_instruction_windows() -> frozenset[str]:
    from .leak import shingles

    return shingles(pass2_instructions())


@lru_cache(maxsize=1)
def instruction_windows() -> frozenset[str]:
    from .leak import shingles

    return shingles(instructions())


def build_system(schema_text: str) -> str:
    return f"{instructions()}\n\n{schema_text}"


def build_user(
    *,
    question: str,
    reference_time: datetime,
    time_zone: str,
    history: Sequence[Turn] = (),
    max_question_chars: int,
    repair_brief: str | None = None,
) -> str:
    local = reference_time.astimezone(ZoneInfo(time_zone))
    parts = [f"Current time: {local.isoformat()} ({time_zone})"]
    recent = list(history)[-MAX_HISTORY_TURNS:]
    if recent:
        lines = ["Earlier in this conversation:"]
        for turn in recent:
            lines.append(f"- question: {untrusted_literal(turn.question, limit=HISTORY_QUESTION_CHARS)}")
            if turn.sql:
                lines.append(f"  sql: {untrusted_literal(turn.sql, limit=HISTORY_SQL_CHARS)}")
            if turn.assumptions:
                items = ", ".join(
                    untrusted_literal(a, limit=HISTORY_ASSUMPTION_CHARS) for a in turn.assumptions[:HISTORY_ASSUMPTIONS]
                )
                lines.append(f"  assumptions: [{items}]")
            intent_type = (turn.output_intent or {}).get("type")
            if isinstance(intent_type, str):
                lines.append(f"  output_type: {untrusted_literal(intent_type, limit=32)}")
        parts.append("\n".join(lines))
    parts.append(f"Question: {untrusted_literal(question, limit=max_question_chars)}")
    if repair_brief:
        parts.append(repair_brief)         # SPEC-09 W1: built by ``repair.render_brief`` from our own text only
    return "\n\n".join(parts)
