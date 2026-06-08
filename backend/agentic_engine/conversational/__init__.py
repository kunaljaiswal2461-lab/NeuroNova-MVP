"""Layer 7 — Conversational Analytics.

Dual-mode chat over a profiled dataset:

  * RAG path — grounds an answer in Layer 6 finding embeddings via the
    cosine-similarity retriever; the LLM never sees raw data.
  * QUERY path — translates a natural-language ask into a single,
    AST-validated Polars expression that runs against the on-disk
    dataset; only read-only operations are allowed.

The :func:`agentic_engine.conversational.chat_agent.stream_chat_turn`
entrypoint orchestrates intent classification, branch execution,
streaming output, and conversation persistence.
"""
