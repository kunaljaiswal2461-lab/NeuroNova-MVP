"""Server-side check and resolution of the pass-2 spec (SPEC-09 W4.4).

The model never writes a figure that reaches the screen: it writes references, and this module replaces each with the
exact string of the value in the result. Any figure it writes anyway must equal a value the model was given (or a
small allowed set), otherwise the sentence is dropped. The first failing hard step returns a ``Fallback`` and the caller
shows the templated summary with the table.

Order of work (the spec's steps, with sanitising moved to the front so the later steps judge the text that will be
shown): envelope, leak, sanitise, references, number check, display fit, mandatory caveats, resolution.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Any

from pydantic import ValidationError

from app.core.logging import get_logger
from app.services.sql_execution import SqlExecutionResult

from .display import NUMERIC_KINDS, canonical_decimal, check_display, kind_of
from .leak import any_instruction_leak
from .pass2_input import STAT_FUNCTIONS, Pass2Input, cell_text, plain
from .pass2_spec import Pass2Spec
from .prompt import PASS2_CANARY, pass2_instruction_windows
from .types import OutputIntent

logger = get_logger("nl_sql.pass2")

DROPPED_NUMBER_CAVEAT = "Some text was removed because it contained a number that is not in the result."
PARTIAL_CAVEAT = "This result is partial (a safety limit was reached). Totals may be incomplete."
MASKED_CAVEAT = "Some columns were hidden from the AI model for privacy."
CONSENT_OFF_CAVEAT = "The AI model did not see row values for this answer (source setting)."
REPAIRED_CAVEAT = "The first query failed and was corrected automatically. Check the assumptions."
MAX_RESOLVED_VALUE_CHARS = 200
MAX_RESOLVED_ANSWER_CHARS = 2_000
ALLOWED_YEARS = range(1900, 2101)
ALLOWED_COUNTS = range(1, 11)

FALLBACK_REASONS = frozenset({
    "spec_invalid", "leak", "bad_reference", "number_check_failed", "llm_unavailable", "input_too_large",
})


@dataclass(frozen=True)
class Fallback:
    reason: str


@dataclass(frozen=True)
class Resolved:
    display: str
    encodings: dict[str, Any]
    table_columns: list[dict[str, str]]
    answer_template: str
    answer: str
    takeaways_template: list[str]
    takeaways: list[str]
    caveats: list[str]
    downgrade: str | None = None            # ``display_downgraded:<from>-><to>``
    derived: dict[str, str] = field(default_factory=dict)
    dropped: int = 0


# ── sanitising ────────────────────────────────────────────────────────────────────────────────────

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_FENCE = re.compile(r"```.*?```|```", re.DOTALL)
_WS = re.compile(r"\s+")


def sanitize(text: str) -> str:
    """Plain text only: no HTML tags, markdown links or images, code fences or control characters."""
    text = _FENCE.sub(" ", text)
    text = _COMMENT.sub(" ", text)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _TAG.sub("", text)
    text = "".join(" " if ch in "\n\r\t\x0b\x0c" else ("" if unicodedata.category(ch) in ("Cc", "Cf") else ch) for ch in text)
    return _WS.sub(" ", text).strip()


# ── references ───────────────────────────────────────────────────────────────────────────────────

_REF = re.compile(r"\{\{(.*?)\}\}")
_ROW_REF = re.compile(r"^row:(\d{1,6})\.(.+)$")
_STAT_REF = re.compile(r"^stat:(.+)\.(" + "|".join(STAT_FUNCTIONS) + r")$")


@dataclass(frozen=True)
class Ref:
    kind: str                     # row | stat | rowcount | delta | delta_pct
    label: str = ""
    index: int = 0
    other: str = ""
    function: str = ""


def parse_ref(body: str) -> Ref | None:
    if body == "rowcount":
        return Ref("rowcount")
    match = _ROW_REF.match(body)
    if match:
        label = match.group(2)
        return None if ("." in label or "}" in label) else Ref("row", label=label, index=int(match.group(1)))
    match = _STAT_REF.match(body)
    if match:
        label = match.group(1)
        return None if ("." in label or "}" in label) else Ref("stat", label=label, function=match.group(2))
    for name in ("delta_pct", "delta"):
        if body.startswith(name + ":"):
            parts = body[len(name) + 1 :].split(",")
            if len(parts) == 2 and all(p and "." not in p and "}" not in p for p in parts):
                return Ref(name, label=parts[0], other=parts[1])
            return None
    return None


# ── numbers ───────────────────────────────────────────────────────────────────────────────────────

_NUMBER = re.compile(r"\d[\d,_]*(?:\.\d+)?(?:[eE][+-]?\d+)?%?")
_WORD = re.compile(r"[A-Za-z]+")
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "dozen": 12,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "million": 1_000_000, "crore": 10_000_000,
           "crores": 10_000_000, "billion": 1_000_000_000, "trillion": 1_000_000_000_000}
_NUMBER_WORDS = frozenset(_UNITS) | frozenset(_TENS) | frozenset(_SCALES) | {"hundred"}


def normalise_digits(text: str) -> str:
    """NFKC, a real minus for U+2212, and every Unicode decimal digit folded to its ASCII digit."""
    text = unicodedata.normalize("NFKC", text).replace("−", "-")
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() and not ch.isascii() else ch for ch in text)


def number_tokens(text: str) -> list[str]:
    """Canonical-decimal identities of every number written in ``text`` as digits or as English number words."""
    text = normalise_digits(text)
    found: list[str] = []
    for match in _NUMBER.finditer(text):
        raw = match.group(0).rstrip(",_").rstrip("%").replace(",", "").replace("_", "")
        negative = (
            match.start() > 0 and text[match.start() - 1] == "-"
            and (match.start() == 1 or text[match.start() - 2] in " \t\n( ")
        )
        identity = canonical_decimal(raw)
        if identity is None:
            found.append("?" + raw)               # never allowed: an unparseable figure cannot be a result value
            continue
        if negative and identity != "0":
            identity = "-" + identity
        found.append(identity)
    found.extend(_word_numbers(text))
    return found


def _word_numbers(text: str) -> list[str]:
    """English number words ("twelve", "two thousand", "one hundred and five") as canonical identities."""
    results: list[str] = []
    run: list[str] = []
    last_end = -1
    pending_and = False

    def flush() -> None:
        nonlocal pending_and
        if run:
            results.append(canonical_decimal(str(_value_of(run))) or "?")
            run.clear()
        pending_and = False

    for match in _WORD.finditer(text):
        word = match.group(0).lower()
        if word in _NUMBER_WORDS:
            if run and match.start() - last_end > 3 and not pending_and:
                flush()                                       # far apart: a separate number
            run.append(word)
            last_end = match.end()
            pending_and = False
        elif word == "and" and run:
            pending_and = True
        else:
            flush()
    flush()
    return results


def _value_of(words: Sequence[str]) -> int:
    total, current = 0, 0
    for word in words:
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        else:
            total += (current or 1) * _SCALES[word]
            current = 0
    return total + current


def is_allowed_count_or_year(identity: str) -> bool:
    if "e-" in identity or identity.startswith("-") or "e" not in identity:
        return False
    digits, exponent = identity.split("e", 1)
    try:
        value = int(digits) * 10 ** int(exponent)
    except (ValueError, OverflowError):
        return False
    return value in ALLOWED_YEARS or value in ALLOWED_COUNTS


def allowed_numbers(
    *, question: str, assumptions: Iterable[str], execution: SqlExecutionResult, inp: Pass2Input
) -> set[str]:
    """Identities of every number the model may write literally."""
    allowed: set[str] = set(number_tokens(question))
    for text in assumptions:
        allowed.update(number_tokens(text))
    allowed.add(canonical_decimal(str(execution.row_count)) or "0")
    allowed.add(canonical_decimal(str(inp.row_count)) or "0")
    for index, column in enumerate(inp.columns):
        if column.personal:
            continue
        for value in column.stats.values():
            identity = canonical_decimal(value)
            if identity is not None:
                allowed.add(identity)
        if inp.consent and column.kind in NUMERIC_KINDS:
            for row in execution.rows[: inp.rows_sent]:
                text = cell_text(row[index])
                identity = canonical_decimal(text) if text is not None else None
                if identity is not None:
                    allowed.add(identity)
    return allowed


def figure_is_allowed(identity: str, allowed: set[str]) -> bool:
    return identity in allowed or is_allowed_count_or_year(identity)


# ── the validation ───────────────────────────────────────────────────────────────────────────────

def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _clean_value(text: str) -> str:
    text = _WS.sub(" ", "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf") or ch in "\n\t")).strip()
    return text if len(text) <= MAX_RESOLVED_VALUE_CHARS else text[: MAX_RESOLVED_VALUE_CHARS - 1] + "…"


def mandatory_caveats(*, complete: bool, masked: bool, consent: bool, repaired: bool) -> list[str]:
    """The cautions code always adds, whatever the model wrote (W4.4 step 6)."""
    out: list[str] = []
    if not complete:
        out.append(PARTIAL_CAVEAT)
    if masked:
        out.append(MASKED_CAVEAT)
    if not consent:
        out.append(CONSENT_OFF_CAVEAT)
    if repaired:
        out.append(REPAIRED_CAVEAT)
    return out


def _delta(current: str, previous: str) -> tuple[str, str]:
    with localcontext() as ctx:
        ctx.prec = 200
        cur, prev = Decimal(current), Decimal(previous)
        delta = cur - prev
        if prev == 0:
            return plain(delta), "n/a"
        pct = ((delta / prev) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return plain(delta), format(pct, "f") + "%"


def validate_and_resolve(
    raw: Any,
    *,
    question: str,
    assumptions: Sequence[str],
    intent: OutputIntent | None,
    execution: SqlExecutionResult,
    inp: Pass2Input,
    repaired: bool,
) -> Resolved | Fallback:
    # 1. Envelope.
    if not isinstance(raw, dict):
        return Fallback("spec_invalid")
    try:
        spec = Pass2Spec.model_validate(raw, strict=True)
    except ValidationError:
        return Fallback("spec_invalid")

    # 2. Canary or a long verbatim run of the instructions: show nothing from pass 2.
    strings = list(_strings(raw))
    if any(PASS2_CANARY in s for s in strings) or any_instruction_leak(strings, pass2_instruction_windows()):
        return Fallback("leak")

    # 3a. Sanitise every text field.
    answer = sanitize(spec.answer)
    takeaways = [t for t in (sanitize(t) for t in spec.takeaways) if t]
    caveats = [c for c in (sanitize(c) for c in spec.caveats) if c]
    if not answer:
        return Fallback("spec_invalid")

    columns = [(c.label, c.pg_type) for c in execution.columns]
    label_index = {label: i for i, (label, _) in enumerate(columns)}
    kinds = [kind_of(t) for _, t in columns]

    # 3b. References. Every one must parse and point at something the model was given.
    refs: list[Ref] = []
    for text in [answer, *takeaways]:
        for match in _REF.finditer(text):
            ref = parse_ref(match.group(1))
            if ref is None:
                return Fallback("bad_reference")
            refs.append(ref)
    if any("{{" in _REF.sub("", t) or "}}" in _REF.sub("", t) for t in [answer, *takeaways, *caveats]):
        return Fallback("bad_reference")
    for ref in refs:
        if ref.kind == "row":
            if not inp.consent or ref.index >= inp.rows_sent or ref.label not in label_index:
                return Fallback("bad_reference")
        elif ref.kind == "stat":
            if inp.stat(ref.label, ref.function) is None:
                return Fallback("bad_reference")
        elif ref.kind in ("delta", "delta_pct"):
            if spec.display != "kpi_comparison" or execution.row_count != 1:
                return Fallback("bad_reference")
            for label in (ref.label, ref.other):
                if label not in label_index or kinds[label_index[label]] not in NUMERIC_KINDS:
                    return Fallback("bad_reference")
                if cell_text(execution.rows[0][label_index[label]]) is None:
                    return Fallback("bad_reference")
    # A placeholder the model saw instead of a personal value must never appear in what is shown.
    visible = [_REF.sub(" ", t) for t in [answer, *takeaways, *caveats]]
    for placeholder in inp.placeholders:
        pattern = re.compile(r"(?<![\w])" + re.escape(placeholder) + r"(?![\w])")
        if any(pattern.search(t) for t in visible):
            return Fallback("bad_reference")

    # 4. Number check. A failing sentence is dropped; an answer with no sentence left falls back.
    allowed = allowed_numbers(question=question, assumptions=assumptions, execution=execution, inp=inp)

    def clean(text: str) -> bool:
        return all(figure_is_allowed(identity, allowed) for identity in number_tokens(_REF.sub(" ", text)))

    dropped = 0
    sentences = [s for s in _SENTENCE.split(answer) if s]
    kept = [s for s in sentences if clean(s)]
    dropped += len(sentences) - len(kept)
    if not kept:
        return Fallback("number_check_failed")
    answer_template = " ".join(kept)
    kept_takeaways = [t for t in takeaways if clean(t)]
    dropped += len(takeaways) - len(kept_takeaways)
    kept_caveats = [c for c in caveats if clean(c)]
    dropped += len(caveats) - len(kept_caveats)

    # 5. Display fit: the model's choice, then the stated intent, then the table.
    names = set(label_index)
    chosen = (spec.display, spec.encodings.model_dump())
    attempts = [chosen]
    if intent is not None:
        attempts.append((intent.type, intent.encodings.model_dump()))
    attempts.append(("table", {"x": None, "y": [], "series": None, "label": None, "value": None}))
    final = None
    for display, encodings in attempts:
        used = [v for k, v in encodings.items() if k != "y" and v is not None] + list(encodings.get("y") or [])
        if any(label not in names for label in used) and display != "table":
            continue
        if check_display(display, encodings, columns, execution.rows).ok:
            final = (display, encodings)
            break
    assert final is not None
    downgrade = None
    extra_caveats: list[str] = []
    if final[0] != spec.display:
        downgrade = f"display_downgraded:{spec.display}->{final[0]}"
        extra_caveats.append(f"Shown as {final[0]} because the result does not fit {spec.display}.")

    # 6. Mandatory caveats, added in code.
    mandatory = mandatory_caveats(
        complete=inp.complete, masked=bool(inp.masked_columns), consent=inp.consent, repaired=repaired
    )
    if dropped:
        mandatory.append(DROPPED_NUMBER_CAVEAT)
    all_caveats = [*mandatory, *extra_caveats, *kept_caveats]

    # 7/8. Resolution: each reference becomes the exact string of the value.
    derived: dict[str, str] = {}
    if final[0] == "kpi_comparison":
        cur_label, prev_label = final[1]["y"][0], final[1]["y"][1]
        current = cell_text(execution.rows[0][label_index[cur_label]])
        previous = cell_text(execution.rows[0][label_index[prev_label]])
        if current is not None and previous is not None:
            try:
                derived["delta"], derived["delta_pct"] = _delta(current, previous)
            except (InvalidOperation, ValueError):
                derived = {}

    def resolve(match: re.Match[str]) -> str:
        ref = parse_ref(match.group(1))
        assert ref is not None
        if ref.kind == "rowcount":
            return str(execution.row_count)
        if ref.kind == "row":
            text = cell_text(execution.rows[ref.index][label_index[ref.label]])
            return "—" if text is None else _clean_value(text)
        if ref.kind == "stat":
            return inp.stat(ref.label, ref.function) or "—"
        current = cell_text(execution.rows[0][label_index[ref.label]])
        previous = cell_text(execution.rows[0][label_index[ref.other]])
        delta, pct = _delta(current or "0", previous or "0")
        return delta if ref.kind == "delta" else pct

    resolved_answer = _REF.sub(resolve, answer_template)[:MAX_RESOLVED_ANSWER_CHARS]
    resolved_takeaways = [_REF.sub(resolve, t) for t in kept_takeaways]

    seen: set[str] = set()
    table_columns: list[dict[str, str]] = []
    for item in spec.table_columns:
        if item.column in label_index and item.column not in seen:
            seen.add(item.column)
            table_columns.append({"column": item.column, "label": _clean_value(sanitize(item.label)) or item.column})
    unreferenceable = [label for label, _ in columns if "." in label or "}" in label]
    if unreferenceable:
        logger.info("nl_sql.pass2_unreferenceable_label", count=len(unreferenceable))
    return Resolved(
        display=final[0],
        encodings=final[1],
        table_columns=table_columns,
        answer_template=answer_template,
        answer=resolved_answer,
        takeaways_template=kept_takeaways,
        takeaways=resolved_takeaways,
        caveats=all_caveats,
        downgrade=downgrade,
        derived=derived,
        dropped=dropped,
    )
