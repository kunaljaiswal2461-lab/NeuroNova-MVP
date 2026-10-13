"""Personal-data heuristics for sample values (SPEC-07 B3, owner decision D3).

These are HEURISTICS. They keep the obvious personal data out of the prompt and the app database; they do not
prove a column is free of it. The owner's per-column exclusion list is the backstop.

Two layers:
* name rules: a column (or its table) *named* like personal data is never read for values;
* value patterns: if any single collected value looks like personal data, the whole column is dropped.

Value matching is done on NFKC-normalised text with Unicode decimal digits folded to ASCII, so full-width
and Devanagari digits and compatibility look-alikes (``＠``) are judged as what they look like.
"""
from __future__ import annotations

import re
import unicodedata

# A name token that marks the column as personal or secret.
PII_NAME_TOKENS = frozenset(
    "email mail phone mobile tel telephone fax address street addr zip zipcode postcode postal pincode pin "
    "ssn aadhaar aadhar pan passport dob birth birthday password passwd pwd token secret salt hash ip ipaddr "
    "iban ifsc card cvv account acct username login".split()
)
# "name" alone is only personal inside a person-like table; so is "<first|last|...>_name".
PERSON_NAME_QUALIFIERS = frozenset("first last full given family middle surname".split())
PERSON_TABLE_TOKENS = frozenset(
    "customer user employee person people contact member patient student client staff".split()
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")


def name_tokens(identifier: str) -> list[str]:
    """Lower-cased tokens of an identifier, split on non-alphanumerics and camelCase boundaries.

    ``customerEmail`` -> ``[customer, email]``; ``"E-mail"`` -> ``[e, mail]``; ``HTTPHost`` -> ``[http, host]``.
    Non-ASCII letters count as separators, so a name written in another script yields no tokens (and is judged
    by its values instead).
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", identifier)
    return [t.lower() for t in _NON_ALNUM.split(spaced) if t]


def table_is_person_like(table_name: str) -> bool:
    tokens = name_tokens(table_name)
    # Tokens plus their plural-stripped form: ``customers`` is a person table, ``employees`` too.
    return any(t in PERSON_TABLE_TOKENS or t.rstrip("s") in PERSON_TABLE_TOKENS for t in tokens)


def column_name_is_pii(table_name: str, column_name: str) -> bool:
    """True when the column name (with its table name) says the values are personal or secret."""
    tokens = name_tokens(column_name)
    if any(t in PII_NAME_TOKENS for t in tokens):
        return True
    if ("name" in tokens or "surname" in tokens) and table_is_person_like(table_name):
        # Bare ``name``, ``first/last/full/given/family/middle`` + ``name``, or ``surname`` (which is a name by
        # itself; the spec's token list names it but a column called just ``surname`` has no ``name`` token).
        return tokens == ["name"] or any(t in PERSON_NAME_QUALIFIERS for t in tokens)
    return False


# ── value patterns ──────────────────────────────────────────────────────────────────────────────────
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_UUID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])", re.I)
_IPV4 = re.compile(r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}(?![0-9])")
_PHONE_STRIP = re.compile(r"[\s\-()+.]")
_DIGIT_RUN = re.compile(r"[0-9]{9,}")
_CARD_RUN = re.compile(r"[0-9]{13,19}")


def _fold(value: str) -> str:
    """NFKC, then any Unicode decimal digit -> its ASCII digit (Devanagari, Arabic-Indic, full-width ...)."""
    text = unicodedata.normalize("NFKC", value)
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() and not ch.isascii() else ch for ch in text)


def value_is_pii(value: str) -> str | None:
    """Return the pattern name when ``value`` looks like personal data, else ``None``."""
    text = _fold(value)
    if _EMAIL.search(text):
        return "email"
    if _UUID.search(text):
        return "uuid"
    if _IPV4.search(text):
        return "ipv4"
    stripped = _PHONE_STRIP.sub("", text)
    # "at least 8 digits after removing spaces, - ( ) + and ." — digits only, so a word with 8 digits and
    # letters ("order12345678") is not a phone, while "+91 98765-43210" is.
    if stripped.isdigit() and len(stripped) >= 8:
        return "phone"
    if _DIGIT_RUN.search(text):
        return "digit_run"
    if _CARD_RUN.search(stripped):
        return "card"
    return None


def any_value_is_pii(values) -> bool:
    return any(value_is_pii(v) is not None for v in values)
