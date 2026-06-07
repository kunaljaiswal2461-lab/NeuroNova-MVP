"""Prompt templates for Layer 5.

Each module exports:
  * ``SYSTEM``         — the role/style instructions, model-agnostic
  * ``build_user(...)``— assembles the user message from a context dict

Prompts are stored as Python (not external files) so they are versioned
with the code, type-checkable, and trivially testable.
"""
