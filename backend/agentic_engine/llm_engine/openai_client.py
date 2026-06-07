"""OpenAI implementation of :class:`LLMClient`.

Uses the official async SDK with built-in retry and timeout handling.
Two call shapes are exposed:

  * :meth:`OpenAIClient.chat_json` — JSON-mode response, used by Layer 5
    where the whole payload is parsed before being persisted.
  * :meth:`OpenAIClient.chat_stream` — token-by-token streaming, used by
    Layer 7's conversational agent so the UI can render the assistant
    reply as it arrives.

Constructed lazily — instantiation does *not* perform a network call, so
it is safe to build at import time and inject as a singleton.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.core.logging import get_logger
from agentic_engine.llm_engine.base_llm import (
    ChatResult,
    LLMUnavailable,
    StreamDelta,
)


logger = get_logger("llm.openai")


_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RETRIES = 2

# Streaming chat completions are typically long-lived (the model is
# producing tokens incrementally). We lift the timeout for stream calls
# so a slow first-token doesn't kill an otherwise-healthy request.
_STREAM_TIMEOUT_SECONDS = 90.0


class OpenAIClient:
    """Thin async wrapper around :class:`openai.AsyncOpenAI`.

    Retries transient errors (rate-limit, timeout, 5xx) automatically via
    the SDK's own retry policy. Hard failures bubble up as
    :class:`LLMUnavailable` so callers can decide whether to degrade or
    propagate.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIClient requires a non-empty api_key")
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    # ── JSON-mode (Layer 5) ──────────────────────────────────────────────

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ) -> ChatResult:
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except (APITimeoutError, RateLimitError, APIError) as exc:
            logger.warning("llm.call_failed", model=model, error=str(exc))
            raise LLMUnavailable(f"OpenAI call failed: {exc}") from exc

        content = response.choices[0].message.content or "{}"
        try:
            parsed: dict[str, Any] = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning(
                "llm.invalid_json",
                model=model,
                snippet=content[:200],
            )
            raise LLMUnavailable(f"OpenAI returned non-JSON content: {exc}") from exc

        usage = response.usage
        return ChatResult(
            content_json=parsed,
            model=response.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    # ── Streaming-mode (Layer 7) ─────────────────────────────────────────

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
        """Yield token deltas, then a terminal delta carrying usage."""
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        if history:
            # Defensive copy: callers may reuse the same list across turns.
            messages.extend(dict(m) for m in history)
        messages.append({"role": "user", "content": user})

        try:
            # ``stream=True`` flips the SDK into incremental mode and asking
            # for ``stream_options.include_usage`` makes OpenAI append a
            # terminal chunk with prompt/completion token counts — without
            # this we would have to count tokens client-side or run a
            # second metadata call.
            stream = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
                timeout=_STREAM_TIMEOUT_SECONDS,
            )
        except (APITimeoutError, RateLimitError, APIError) as exc:
            logger.warning("llm.stream_open_failed", model=model, error=str(exc))
            raise LLMUnavailable(f"OpenAI stream failed to open: {exc}") from exc

        resolved_model = model
        input_tokens = 0
        output_tokens = 0

        try:
            async for chunk in stream:
                # The model identifier is echoed on every chunk; capture it
                # the first time we see it so the terminal delta reflects
                # whatever OpenAI actually routed us to (e.g. dated alias).
                if getattr(chunk, "model", None):
                    resolved_model = chunk.model

                # Usage chunks (the trailing frame produced by
                # ``include_usage``) have no choices but populate
                # ``chunk.usage``.
                if getattr(chunk, "usage", None) is not None:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0
                    continue

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None) or ""
                if text:
                    yield StreamDelta(text=text, finished=False)
        except (APITimeoutError, RateLimitError, APIError) as exc:
            logger.warning("llm.stream_failed", model=model, error=str(exc))
            raise LLMUnavailable(f"OpenAI stream failed: {exc}") from exc

        # Terminal frame — exactly one, regardless of how many text deltas
        # we yielded. Callers rely on its presence to know the turn
        # completed without error.
        yield StreamDelta(
            text="",
            finished=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=resolved_model,
        )

    async def aclose(self) -> None:
        await self._client.close()


def build_client(api_key: str | None) -> OpenAIClient | None:
    """Build an :class:`OpenAIClient` if a key is configured, else ``None``.

    Returning ``None`` lets layer orchestrators gracefully degrade when
    the operator has not provided ``OPENAI_API_KEY`` (e.g. in CI or
    local dev). Layer 5 and Layer 7 both branch on this.
    """
    if not api_key:
        return None
    return OpenAIClient(api_key=api_key)
