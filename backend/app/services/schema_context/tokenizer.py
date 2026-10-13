"""Offline ``o200k_base`` token counter (SPEC-06 B4, F5).

``tiktoken`` normally downloads the vocabulary on first use. Here the file is vendored and read from disk, so
counting never opens a socket. The expected hash and the pattern/special tokens are copied from
``tiktoken_ext.openai_public.o200k_base``; a test compares them with the installed definition.
A corrupted or swapped vocabulary file raises when this module is imported.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

import tiktoken
from tiktoken.load import load_tiktoken_bpe

VOCAB_PATH = Path(__file__).parent / "resources" / "o200k_base.tiktoken"
VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
PAT_STR = "|".join(
    [
        r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
        r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
        r"""\p{N}{1,3}""",
        r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
        r"""\s*[\r\n]+""",
        r"""\s+(?!\S)""",
        r"""\s+""",
    ]
)
SPECIAL_TOKENS = {"<|endoftext|>": 199999, "<|endofprompt|>": 200018}


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class TiktokenCounter:
    def __init__(self) -> None:
        # tiktoken skips its own hash check when the cache is disabled and trusts a cached copy keyed by path,
        # so the vendored file is verified here, on the bytes that are actually going to be parsed.
        if hashlib.sha256(VOCAB_PATH.read_bytes()).hexdigest() != VOCAB_SHA256:
            raise ValueError(f"vendored vocabulary {VOCAB_PATH.name} does not match its expected sha256")
        ranks = load_tiktoken_bpe(str(VOCAB_PATH), expected_hash=VOCAB_SHA256)
        self._encoding = tiktoken.Encoding(
            name="o200k_base", pat_str=PAT_STR, mergeable_ranks=ranks, special_tokens=SPECIAL_TOKENS
        )

    def count(self, text: str) -> int:
        # Schema text is data: special-token look-alikes count as ordinary characters.
        return len(self._encoding.encode(text, disallowed_special=()))


O200K_COUNTER: TokenCounter = TiktokenCounter()
