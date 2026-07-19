"""Placeholder for an NL→Pandas execution path.

The MVP's NL-query branch is implemented in ``query_executor.py`` against
Polars, with its own AST whitelist, sandboxed eval, and wall-clock
timeout. A separate Pandas executor does NOT exist in this codebase.

Per the security implementation plan: when a feature being secured does
not yet exist, ship a clearly labelled placeholder that raises rather
than build the missing feature. If/when an actual Pandas path is added,
it must replicate the same three-rule contract:

    1. AST whitelist + name blocklist (imports, dunders, os/sys, eval,
       exec, subprocess, file access).
    2. Threaded execution with a 10-second wall-clock timeout.
    3. Result coercion — DataFrame >10k rows truncated to 10k, Series
       converted to DataFrame, missing ``result`` raises a descriptive
       error.

The execution scope must expose only ``pandas`` (and optionally
``numpy``) plus the input DataFrame; ``__builtins__`` must be stripped.
"""
from __future__ import annotations

from typing import Any


_PLACEHOLDER_MESSAGE = (
    "NL→Pandas execution is not implemented in this build. "
    "The active NL-query path uses Polars and lives in "
    "agentic_engine.conversational.query_executor.execute_query. "
    "If a Pandas executor is added, it must enforce AST whitelist, "
    "a 10s thread timeout, 10k-row result truncation, and a sandbox "
    "with builtins stripped."
)


def execute_pandas_safely(code: str, df: Any) -> Any:  # pragma: no cover - placeholder
    """Reserved name for the future safe Pandas executor.

    Raises ``NotImplementedError`` unconditionally. The signature is
    fixed so callers can be wired ahead of the real implementation.
    """
    raise NotImplementedError(_PLACEHOLDER_MESSAGE)


__all__ = ["execute_pandas_safely"]
