"""Record/replay LLM client (SPEC-03 §14).

``replay`` returns stored responses and never touches the network; a missing
entry raises :class:`CassetteMiss` (INV-P0-06). ``record`` wraps a real client,
requires the synthetic-data ACK and a fixture environment (INV-P0-07), and
writes the file atomically.

Only the six request fields in the key plus the response are ever stored — no
API keys, headers or client configuration.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal

from agentic_engine.llm_engine.base_llm import ChatResult, LLMClient, StreamDelta
from evals import guards

ACK_ENV = "STAGE4C_EVALUATION_ACK"
ACK_VALUE = "disposable-synthetic-only"

_KEY_FIELDS = ("method", "model", "system", "user", "history", "max_tokens", "temperature")


class CassetteError(RuntimeError):
    pass


class CassetteMiss(CassetteError):
    pass


def _request(method: str, *, model, system, user, history, max_tokens, temperature) -> dict[str, Any]:
    return {
        "method": method,
        "model": model,
        "system": system,
        "user": user,
        "history": [dict(h) for h in (history or [])],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def request_key(request: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {k: request[k] for k in _KEY_FIELDS}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CassetteLLM:
    """Implements :class:`agentic_engine.llm_engine.base_llm.LLMClient`."""

    def __init__(
        self,
        path: Path,
        mode: Literal["record", "replay"],
        inner: LLMClient | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ):
        self.path = Path(path)
        self.mode = mode
        self._inner = inner
        if mode == "replay":
            if inner is not None:
                raise CassetteError("replay mode must not be given an inner client (INV-P0-06)")
            if not self.path.exists():
                raise CassetteError(f"cassette not found: {self.path}; record it first")
        elif mode == "record":
            if inner is None:
                raise CassetteError("record mode needs an inner client")
            env = os.environ if env is None else env
            if env.get(ACK_ENV) != ACK_VALUE:
                raise CassetteError(f"recording requires {ACK_ENV}={ACK_VALUE} (INV-P0-07)")
            guards.assert_fixture_environment(env)
        else:
            raise CassetteError(f"unknown mode {mode!r}")
        self._entries: dict[str, dict] = self._load()

    # ── storage ──────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    entries[entry["key"]] = entry
        return entries

    def _write_atomic(self, entries: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for entry in entries.values():
                    fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _store(self, request: dict, response: Any, model: str, input_tokens: int, output_tokens: int) -> None:
        key = request_key(request)
        entries = dict(self._entries)
        entries[key] = {
            "key": key,
            "request": request,
            "response": response,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        self._write_atomic(entries)       # only adopt the new state once it is on disk
        self._entries = entries

    def _lookup(self, request: dict) -> dict:
        entry = self._entries.get(request_key(request))
        if entry is not None:
            return entry
        raise CassetteMiss(self._miss_message(request))

    def _miss_message(self, request: dict) -> str:
        if not self._entries:
            return f"no entry in {self.path.name} (cassette is empty); re-record this cassette"
        nearest = max(
            self._entries.values(),
            key=lambda e: difflib.SequenceMatcher(None, e["request"]["system"], request["system"]).ratio(),
        )
        changed = [k for k in _KEY_FIELDS if nearest["request"].get(k) != request.get(k)]
        diff = ""
        if "system" in changed:
            lines = difflib.unified_diff(
                nearest["request"]["system"].splitlines(),
                request["system"].splitlines(),
                "recorded system", "current system", lineterm="", n=0,
            )
            diff = "\n".join(list(lines)[:12])
        return (
            f"no recorded response in {self.path.name} for this request; re-record this cassette.\n"
            f"nearest stored entry {nearest['key'][:12]} differs in: {', '.join(changed) or 'nothing?'}\n{diff}"
        ).rstrip()

    # ── LLMClient ────────────────────────────────────────────────────────────

    async def chat_json(
        self, *, model: str, system: str, user: str, max_tokens: int = 1500, temperature: float = 0.2
    ) -> ChatResult:
        request = _request(
            "chat_json", model=model, system=system, user=user, history=None,
            max_tokens=max_tokens, temperature=temperature,
        )
        if self.mode == "replay":
            e = self._lookup(request)
            return ChatResult(
                content_json=e["response"], model=e["model"],
                input_tokens=e["input_tokens"], output_tokens=e["output_tokens"],
            )
        result = await self._inner.chat_json(
            model=model, system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
        self._store(request, result.content_json, result.model, result.input_tokens, result.output_tokens)
        return result

    async def chat_stream(
        self,
        *,
        model: str,
        system: str,
        user: str,
        history: list[dict[str, str]] | None = None,
        max_tokens: int = 1500,
        temperature: float = 0.4,
    ) -> AsyncIterator[StreamDelta]:
        request = _request(
            "chat_stream", model=model, system=system, user=user, history=history,
            max_tokens=max_tokens, temperature=temperature,
        )
        if self.mode == "replay":
            e = self._lookup(request)
            for text in e["response"]:
                yield StreamDelta(text=text)
            yield StreamDelta(
                finished=True, input_tokens=e["input_tokens"], output_tokens=e["output_tokens"], model=e["model"]
            )
            return
        deltas: list[str] = []
        terminal: StreamDelta | None = None
        async for delta in self._inner.chat_stream(
            model=model, system=system, user=user, history=history,
            max_tokens=max_tokens, temperature=temperature,
        ):
            if delta.finished:
                terminal = delta
            elif delta.text:
                deltas.append(delta.text)
            yield delta
        if terminal is not None:         # an errored stream has no terminal delta: record nothing
            self._store(request, deltas, terminal.model or model, terminal.input_tokens, terminal.output_tokens)
