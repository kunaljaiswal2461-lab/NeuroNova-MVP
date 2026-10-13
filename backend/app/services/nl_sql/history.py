"""Earlier turns of the conversation as pass-1 context (SPEC-09 W6). Rows are never included."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .prompt import MAX_HISTORY_TURNS
from .types import Turn


def _role(message: Any) -> str:
    role = getattr(message, "role", "")
    return str(getattr(role, "value", role)).lower()


def _nl_sql(message: Any) -> dict[str, Any] | None:
    record = getattr(message, "nl_sql", None)
    if record is None:
        return None
    return record.model_dump(mode="json") if hasattr(record, "model_dump") else dict(record)


def _metric_name(message: Any) -> str | None:
    result = getattr(message, "postgres_result", None)
    provenance = getattr(result, "metric_provenance", None)
    name = getattr(provenance, "name", None)
    return name if isinstance(name, str) and name else None


def history_from_messages(messages: Sequence[Any]) -> list[Turn]:
    """Pair each user message with the assistant message that follows it; keep the last ``MAX_HISTORY_TURNS``."""
    turns: list[Turn] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if _role(message) != "user":
            index += 1
            continue
        question = str(getattr(message, "content", ""))
        answer = None
        probe = index + 1
        while probe < len(messages) and _role(messages[probe]) != "user":
            if _role(messages[probe]) == "assistant":
                answer = messages[probe]
                break
            probe += 1
        index = probe if answer is None else probe + 1
        if answer is None:
            continue                                    # a question that never got an answer is not context
        record = _nl_sql(answer)
        if record is not None:
            outcome = record.get("outcome", "failed")
            if outcome == "answer":
                turns.append(Turn(
                    question=question, sql=record.get("sql"), assumptions=tuple(record.get("assumptions") or ()),
                    output_intent=record.get("output_intent"), outcome="answer",
                ))
            elif outcome == "clarification":
                asked = str(getattr(answer, "content", ""))
                turns.append(Turn(question=question, assumptions=(f"asked: {asked}",), outcome="clarification"))
            else:
                turns.append(Turn(question=question, outcome=outcome))      # unsupported or failed
            continue
        if getattr(answer, "unsupported", False):
            turns.append(Turn(question=question, outcome="unsupported"))
            continue
        metric = _metric_name(answer)
        if metric is not None and not getattr(answer, "degraded", False):
            turns.append(Turn(question=question, assumptions=(f"used confirmed metric {metric}",), outcome="answer"))
        else:
            turns.append(Turn(question=question, outcome="failed" if getattr(answer, "degraded", False) else "answer"))
    return turns[-MAX_HISTORY_TURNS:]
