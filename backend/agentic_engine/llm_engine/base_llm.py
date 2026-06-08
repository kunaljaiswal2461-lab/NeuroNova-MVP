"""LLM client abstraction.

Defines the structural contract (``LLMClient`` Protocol) that every layer
that talks to a chat model depends on. Production code uses
:class:`OpenAIClient`; tests inject a stub that records prompts and
returns canned content. This seam is what makes Layers 5 and 7
unit-testable without hitting OpenAI.

The Protocol covers two call shapes:

  * :meth:`LLMClient.chat_json` — single round-trip, JSON-mode (Layer 5).
  * :meth:`LLMClient.chat_stream` — token-by-token streaming used by
    Layer 7's conversational agent. Yields :class:`StreamDelta` events:
    zero or more text deltas, then exactly one final delta carrying token
    usage so callers can aggregate cost without a separate metadata call.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# ── result/event types ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ChatResult:
    """Outcome of a single non-streaming LLM chat call.

    ``content_json`` is the already-parsed JSON object returned by the
    model. Token counts are taken from the provider's response metadata
    so the orchestrator can aggregate cost across the calls that make up
    an InsightReport or a chat turn.
    """

    content_json: dict
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class StreamDelta:
    """One frame in a streaming chat response.

    The stream yields zero or more deltas with ``finished=False`` and a
    non-empty ``text`` payload, followed by exactly one terminal delta
    with ``finished=True`` carrying the final token counts and model id.
    The terminal delta carries an empty text payload — callers should
    accumulate text only from non-terminal deltas.
    """

    text: str = ""
    finished: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class LLMUnavailable(Exception):
    """Raised when the configured LLM provider cannot be reached.

    Layer-level orchestrators catch this and emit a degraded report
    rather than failing the whole pipeline. Layer 7 surfaces it as a
    streaming ``error`` event so the UI can render a fallback message
    without dropping the connection mid-stream.
    """


# ── Protocol seam ────────────────────────────────────────────────────────────

@runtime_checkable
class LLMClient(Protocol):
    """Async chat client supporting both JSON and streaming modes.

    Implementations must:

      * return parsed JSON (not a string) from :meth:`chat_json` so the
        orchestrator can hand the payload straight to Pydantic;
      * implement their own retry/backoff policy;
      * raise :class:`LLMUnavailable` on permanent failure.
    """

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ) -> ChatResult:
        ...

    def chat_stream(
        self,
        *,
        model: str,
        system: str,
        user: str,
        history: list[dict[str, str]] | None = None,
        max_tokens: int = 1500,
        temperature: float = 0.4,
    ) -> AsyncIterator[StreamDelta]:
        """Stream a chat response as a sequence of :class:`StreamDelta`.

        ``history`` is a list of ``{"role": "user" | "assistant", "content": str}``
        dicts prepended after the ``system`` message and before the new
        ``user`` turn, so multi-turn context flows in without the caller
        having to reshape it.

        Implementations should yield deltas as they arrive and emit the
        terminal ``finished=True`` delta before returning. They must not
        yield the terminal delta if the stream errored out — callers rely
        on its presence to know the turn completed cleanly.
        """
        ...
