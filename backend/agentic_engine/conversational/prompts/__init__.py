"""Layer 7 prompts.

Kept here (under ``conversational/``) rather than under
``llm_engine/prompts/`` because they are conversational concerns —
intent disambiguation, RAG answering, NL→Polars codegen — and have no
overlap with Layer 5's prompts beyond the shared LLMClient seam.

Every prompt module exposes a ``SYSTEM`` constant plus a ``build_user``
function so the chat agent can assemble the call without duplicating
formatting logic at the call site (mirrors the Layer 5 prompts layout).
"""
