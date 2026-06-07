"""Text → vector adapters.

Defines a minimal :class:`Embedder` Protocol that the indexer and
retriever depend on, plus an OpenAI implementation. The Protocol is the
test seam: a ``FakeEmbedder`` in unit tests can return deterministic
vectors without hitting the network.
"""
from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.core.logging import get_logger
from agentic_engine.llm_engine.base_llm import LLMUnavailable


logger = get_logger("vector_store.embedder")


# OpenAI's embeddings endpoint accepts up to 2048 inputs per request and
# ~300k tokens of total input. We chunk well below those caps to keep
# memory predictable and to let individual retries be cheap.
_MAX_BATCH_INPUTS: int = 256

# Dimensionality of ``text-embedding-3-small`` (production default).
# ``text-embedding-3-large`` would be 3072; if that model is ever wired
# up we will also need to bump the SQL column and migration.
_OPENAI_SMALL_DIM: int = 1536


@runtime_checkable
class Embedder(Protocol):
    """Async text → vector contract."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    """OpenAI async embedder with built-in batching and retry.

    ``AsyncOpenAI`` already retries transient errors (429 / 5xx / timeout)
    via its ``max_retries`` knob; we surface a deterministic
    :class:`LLMUnavailable` on permanent failure so callers can decide
    whether to degrade or propagate.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "text-embedding-3-small",
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIEmbedder requires a non-empty api_key")
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._model = model
        # Dimensionality is fixed per model — declared up-front so callers
        # can size their pgvector columns correctly at startup.
        self._dimension = _OPENAI_SMALL_DIM

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts in batched calls, preserving input order."""
        if not texts:
            return []

        out: list[list[float]] = []
        for batch in _batched(texts, _MAX_BATCH_INPUTS):
            try:
                response = await self._client.embeddings.create(
                    model=self._model,
                    input=batch,
                )
            except (APITimeoutError, RateLimitError, APIError) as exc:
                logger.warning(
                    "embedder.call_failed",
                    model=self._model,
                    batch_size=len(batch),
                    error=str(exc),
                )
                raise LLMUnavailable(f"OpenAI embed call failed: {exc}") from exc

            # OpenAI returns data sorted by ``index``, but the docs do not
            # contractually guarantee order — sort defensively so the
            # output aligns with the input slice.
            ordered = sorted(response.data, key=lambda d: d.index)
            out.extend(d.embedding for d in ordered)

        return out

    async def aclose(self) -> None:
        await self._client.close()


def build_embedder(api_key: str | None) -> OpenAIEmbedder | None:
    """Return a configured embedder, or ``None`` when no key is present.

    Mirrors :func:`agentic_engine.llm_engine.openai_client.build_client` so
    the indexer can degrade gracefully when running in environments
    without an OpenAI key (CI, local dev).
    """
    if not api_key:
        return None
    return OpenAIEmbedder(api_key=api_key)


def _batched(items: list[str], size: int) -> Iterable[list[str]]:
    """Yield successive ``size``-length slices of ``items``."""
    for i in range(0, len(items), size):
        yield items[i:i + size]
