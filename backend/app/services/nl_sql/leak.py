"""Instruction-leak check (SPEC-09 W0.4): a model string that repeats a long verbatim run of the system instructions.

The canary catches a model that prints the marker. A model that copies or lightly rewords the rules leaks nothing the
canary can see, so any 40-character run of the (whitespace-normalised) instructions found in a model string counts as
a leak. The shingle set is built once per instruction text. Text inside backticks is left out of the set: the prompt
shows worked SQL examples there, and a correct answer can legitimately contain the same clause.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

SHINGLE_CHARS = 40

_WS = re.compile(r"\s+")
_CODE_SPAN = re.compile(r"`[^`]*`")


def _normalise(text: str) -> str:
    return _WS.sub(" ", text).strip()


@lru_cache(maxsize=16)
def shingles(instructions: str, size: int = SHINGLE_CHARS) -> frozenset[str]:
    """Every ``size``-character window of each prose run of ``instructions`` (code spans removed)."""
    found: set[str] = set()
    for run in _CODE_SPAN.sub("\n", instructions).split("\n"):
        run = _normalise(run)
        for i in range(len(run) - size + 1):
            found.add(run[i : i + size])
    return frozenset(found)


def contains_instruction_run(text: str, windows: frozenset[str], size: int = SHINGLE_CHARS) -> bool:
    text = _normalise(text)
    return any(text[i : i + size] in windows for i in range(len(text) - size + 1))


def any_instruction_leak(strings: Iterable[str], windows: frozenset[str], size: int = SHINGLE_CHARS) -> bool:
    return any(contains_instruction_run(s, windows, size) for s in strings)
