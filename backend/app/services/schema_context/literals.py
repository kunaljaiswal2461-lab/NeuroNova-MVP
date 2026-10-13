"""Identifier quoting and the untrusted-string fence (SPEC-06 B2 rules 2, 3, 8)."""
from __future__ import annotations

import json
import re
import unicodedata

from .keywords import QUOTED_KEYWORDS

NOTE_LIMIT = 240
SAMPLE_VALUE_LIMIT = 64
MAX_SAMPLE_VALUES = 20

_BARE = re.compile(r"^[a-z_][a-z0-9_$]*$")
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def is_unsafe_char(ch: str) -> bool:
    return unicodedata.category(ch) in _UNSAFE_CATEGORIES


def is_unsafe_text(value: str) -> bool:
    """True when ``value`` has a control, format, or line/paragraph-separator character."""
    return any(is_unsafe_char(ch) for ch in value)


def quote_identifier(name: str) -> str:
    """Spell ``name`` the way ``quote_ident`` would."""
    if _BARE.match(name) and name not in QUOTED_KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def _escape_char(ch: str) -> str:
    code = ord(ch)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


def untrusted_literal(value: str, *, limit: int = NOTE_LIMIT) -> str:
    """One JSON string literal on one line, for text copied from the database (rule 8).

    NUL is stripped, the text is cut to ``limit`` code points (the last one ``…`` when cut), and every raw
    Cc/Cf/Zl/Zp (and lone surrogate) is written as a ``\\uXXXX`` escape. The result contains no raw newline
    or unescaped quote, so it cannot end its own line or imitate a header, table or fence.
    """
    value = value.replace("\x00", "")
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    encoded = json.dumps(value, ensure_ascii=False)
    return "".join(
        _escape_char(ch) if (is_unsafe_char(ch) or unicodedata.category(ch) == "Cs") else ch for ch in encoded
    )
