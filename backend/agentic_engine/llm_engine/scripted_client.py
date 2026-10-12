"""A scripted ``chat_structured`` client for development and tests (SPEC-09 acceptance, ``NL_SQL_STUB_LLM_PATH``).

The script is a JSONL file. Each line answers one kind of call:

    {"schema": "pass1_envelope", "match": "orders by region", "response": {...envelope...}}
    {"schema": "pass1_envelope", "match": "orders by region", "brief": true, "response": {...}}   # the repair call
    {"schema": "pass2_spec", "match": "orders by region", "response": {...spec...}}
    {"schema": "pass1_envelope", "match": "slow", "error": "unavailable"}
    {"schema": "pass2_spec", "match": "x", "finish_reason": "refusal"}

``match`` is a case-insensitive substring (or, with ``"exact": true``, the whole question) of the question line in the user message. The first line that fits wins.
No line fits -> ``LLMUnavailable``. The file is read when the client is built, so editing it takes effect on the next
turn. Nothing here reaches a network.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentic_engine.llm_engine.base_llm import ChatResult, LLMUnavailable

_QUESTION = re.compile(r"^Question: (.*)$", re.MULTILINE)
_BRIEF = "Your previous answer could not be used."


class ScriptedLLM:
    def __init__(self, path: str | Path) -> None:
        self._lines: list[dict[str, Any]] = []
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if raw.strip() and not raw.lstrip().startswith("#"):
                self._lines.append(json.loads(raw))
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(self, *, user: str, schema_name: str, **kwargs: Any) -> ChatResult:
        self.calls.append({"schema_name": schema_name, "user": user, **kwargs})
        found = _QUESTION.search(user)
        question = json.loads(found.group(1)).lower() if found else user.lower()
        has_brief = _BRIEF in user
        for line in self._lines:
            if line.get("schema") != schema_name:
                continue
            wanted = str(line.get("match", "")).lower()
            if (question != wanted) if line.get("exact") else (wanted not in question):
                continue
            if "brief" in line and bool(line["brief"]) != has_brief:
                continue
            if line.get("error") == "unavailable":
                raise LLMUnavailable()
            finish = line.get("finish_reason", "stop")
            return ChatResult(
                content_json=line.get("response", {}) if finish == "stop" else {},
                model=str(line.get("model", "scripted-stub")),
                input_tokens=int(line.get("input_tokens", 100)),
                output_tokens=int(line.get("output_tokens", 20)),
                finish_reason=finish,
            )
        raise LLMUnavailable()

    async def chat_json(self, **_kwargs: Any) -> ChatResult:
        raise LLMUnavailable()

    def chat_stream(self, **_kwargs: Any):
        raise LLMUnavailable()
