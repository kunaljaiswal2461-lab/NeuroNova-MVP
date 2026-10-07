"""Reports, aggregate maths, baselines and the regression gate (SPEC-03 §4, §13.5, §13.6)."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from evals.grader import PASSING, Verdict
from evals.models import EVALS_DIR

BASELINES_DIR = EVALS_DIR / "baselines"
REPORTS_DIR = EVALS_DIR / "reports"
PRICING_PATH = EVALS_DIR / "pricing.yaml"

Z95 = 1.96
TAG_WARN_DROP = 0.10
MUTANT_KILL_TARGET = 0.95


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── accuracy maths (§4) ──────────────────────────────────────────────────────

class Accuracy(_Model):
    p: float | None            # None when there is nothing to measure
    n: int                     # distinct questions behind the number
    runs: int                  # counted runs (ambiguous-but-answered runs excluded)
    half_interval: float       # 95% half-width in absolute terms (0.065 = ±6.5 points)


def interval_half_width(p: float, n: int) -> float:
    """95% half-interval from the standard error ``sqrt(p(1-p)/n)``."""
    if n <= 0:
        return 0.0
    return Z95 * math.sqrt(max(p * (1 - p), 0.0) / n)


def accuracy_of(records: list["RunRecord"]) -> Accuracy:
    counted = [r for r in records if r.verdict is not Verdict.ANSWERED_AMBIGUOUS_REASONABLE]
    if not counted:
        return Accuracy(p=None, n=0, runs=0, half_interval=0.0)
    passed = sum(r.verdict in PASSING for r in counted)
    p = passed / len(counted)
    n = len({r.question_id for r in counted})
    return Accuracy(p=p, n=n, runs=len(counted), half_interval=interval_half_width(p, n))


# ── per-run and report schemas ───────────────────────────────────────────────

class RunRecord(_Model):
    question_id: str
    tags: list[str]
    output_type: str | None = None
    expected_kind: Literal["answer", "clarification", "refusal"]
    run_index: int
    verdict: Verdict
    reason: str = ""
    sql: str | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    models: list[str] = Field(default_factory=list)
    examples: list[tuple[str, str]] = Field(default_factory=list)      # synthetic fixture rows only


class Aggregates(_Model):
    accuracy: Accuracy
    by_tag: dict[str, Accuracy]
    by_output_type: dict[str, Accuracy]
    verdict_counts: dict[str, int]
    consistency: float | None           # share of questions whose k runs all got one verdict
    ambiguous_answered_rate: float | None
    critical_count: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None


class RunMeta(_Model):
    git_commit: str | None
    fixture_version: int
    candidate: str
    suite: str
    models: list[str]
    repeat: int
    started_at: str
    finished_at: str
    extras: dict[str, Any] = Field(default_factory=dict)


class Report(_Model):
    meta: RunMeta
    aggregates: Aggregates
    records: list[RunRecord]


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))                 # nearest rank
    return ordered[rank - 1]


def load_pricing(path: Path = PRICING_PATH) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("models", {}) or {}


def estimate_cost(records: list[RunRecord], pricing: dict[str, dict[str, float]]) -> float | None:
    """Omitted (``None``) when any model used has no price entry."""
    total = 0.0
    seen = False
    for r in records:
        if not r.models:
            continue
        for model in set(r.models):
            price = pricing.get(model)
            if not price:
                return None
        model = r.models[0]
        price = pricing[model]
        total += r.input_tokens / 1e6 * price["input_per_mtok"] + r.output_tokens / 1e6 * price["output_per_mtok"]
        seen = True
    return round(total, 6) if seen else None


def aggregate(records: list[RunRecord], pricing: dict[str, dict[str, float]] | None = None) -> Aggregates:
    by_tag_records: dict[str, list[RunRecord]] = defaultdict(list)
    by_type_records: dict[str, list[RunRecord]] = defaultdict(list)
    by_question: dict[str, list[RunRecord]] = defaultdict(list)
    for r in records:
        for tag in r.tags:
            by_tag_records[tag].append(r)
        if r.output_type:
            by_type_records[r.output_type].append(r)
        by_question[r.question_id].append(r)

    multi = [rs for rs in by_question.values() if len(rs) > 1]
    consistency = (
        sum(len({r.verdict for r in rs}) == 1 for rs in multi) / len(multi) if multi else None
    )
    clarif = [r for r in records if r.expected_kind == "clarification"]
    ambiguous_rate = (
        sum(r.verdict is Verdict.ANSWERED_AMBIGUOUS_REASONABLE for r in clarif) / len(clarif) if clarif else None
    )
    latencies = [r.latency_ms for r in records if r.latency_ms]
    return Aggregates(
        accuracy=accuracy_of(records),
        by_tag={t: accuracy_of(rs) for t, rs in sorted(by_tag_records.items())},
        by_output_type={t: accuracy_of(rs) for t, rs in sorted(by_type_records.items())},
        verdict_counts=dict(sorted(Counter(r.verdict.value for r in records).items())),
        consistency=consistency,
        ambiguous_answered_rate=ambiguous_rate,
        critical_count=sum(r.verdict is Verdict.CRITICAL_WRITE_ANSWERED for r in records),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        input_tokens=sum(r.input_tokens for r in records),
        output_tokens=sum(r.output_tokens for r in records),
        estimated_cost_usd=estimate_cost(records, pricing if pricing is not None else load_pricing()),
    )


# ── markdown summary ─────────────────────────────────────────────────────────

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def render_markdown(report: Report, *, worst_tags: int = 5) -> str:
    a = report.aggregates
    acc = a.accuracy
    lines = [
        f"## {report.meta.candidate} on {report.meta.suite} (fixture v{report.meta.fixture_version}, k={report.meta.repeat})",
        "",
        f"- **Execution accuracy:** {_pct(acc.p)} ± {acc.half_interval * 100:.1f} pts "
        f"(95%, {acc.n} questions, {acc.runs} runs)",
        f"- **CRITICAL (write answered):** {a.critical_count}",
    ]
    if a.consistency is not None:
        lines.append(f"- **Consistency across runs:** {_pct(a.consistency)}")
    if a.ambiguous_answered_rate is not None:
        lines.append(f"- **Guessed instead of asking (ambiguous questions):** {_pct(a.ambiguous_answered_rate)}")
    if a.latency_p50_ms is not None:
        lines.append(f"- **Latency:** p50 {a.latency_p50_ms:.0f} ms, p95 {a.latency_p95_ms:.0f} ms")
    if a.input_tokens or a.output_tokens:
        cost = "" if a.estimated_cost_usd is None else f", ≈ ${a.estimated_cost_usd:.4f}"
        lines.append(f"- **Tokens:** {a.input_tokens} in / {a.output_tokens} out{cost}")
    worst = sorted(
        (t for t in a.by_tag.items() if t[1].p is not None), key=lambda kv: (kv[1].p, kv[0])
    )[:worst_tags]
    if worst:
        lines += ["", "| Worst tags | Accuracy | ± | Questions |", "|---|---|---|---|"]
        lines += [f"| {t} | {_pct(v.p)} | {v.half_interval * 100:.1f} | {v.n} |" for t, v in worst]
    failing: dict[str, list[str]] = defaultdict(list)
    for r in report.records:
        if r.verdict not in PASSING and r.verdict is not Verdict.ANSWERED_AMBIGUOUS_REASONABLE:
            failing[r.verdict.value].append(r.question_id)
    if failing:
        lines += ["", "**Failing questions by verdict**", ""]
        for verdict, ids in sorted(failing.items()):
            lines.append(f"- `{verdict}`: {', '.join(sorted(set(ids)))}")
    return "\n".join(lines)


def write_report(report: Report, directory: Path = REPORTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report.meta.started_at.replace(":", "").replace("-", "")[:15]
    path = directory / f"{stamp}-{report.meta.suite}-{report.meta.candidate}.json"
    path.write_text(report.model_dump_json(indent=2))
    return path


# ── baselines and the regression gate (§13.6) ────────────────────────────────

class BaselineTag(_Model):
    p: float | None
    n: int


class Baseline(_Model):
    """Aggregates only. No SQL, no rows."""

    candidate: str
    suite: str
    fixture_version: int
    repeat: int
    accuracy: Accuracy
    by_tag: dict[str, BaselineTag]
    critical_count: int
    extras: dict[str, Any] = Field(default_factory=dict)      # e.g. mutants: equivalence/kill rates


def baseline_from_report(report: Report) -> Baseline:
    a = report.aggregates
    return Baseline(
        candidate=report.meta.candidate,
        suite=report.meta.suite,
        fixture_version=report.meta.fixture_version,
        repeat=report.meta.repeat,
        accuracy=a.accuracy,
        by_tag={t: BaselineTag(p=v.p, n=v.n) for t, v in a.by_tag.items()},
        critical_count=a.critical_count,
        extras=report.meta.extras,
    )


def baseline_path(candidate: str, directory: Path = BASELINES_DIR) -> Path:
    safe = candidate.replace(":", "_").replace("/", "_")
    return directory / f"{safe}.json"


def write_baseline(baseline: Baseline, directory: Path = BASELINES_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = baseline_path(baseline.candidate, directory)
    path.write_text(baseline.model_dump_json(indent=2) + "\n")
    return path


def load_baseline(candidate: str, directory: Path = BASELINES_DIR) -> Baseline | None:
    path = baseline_path(candidate, directory)
    return Baseline.model_validate_json(path.read_text()) if path.exists() else None


def compare_baseline(report: Report, baseline: Baseline | None) -> tuple[int, list[str]]:
    """Return ``(exit_code, messages)``. Non-zero blocks the change."""
    a = report.aggregates
    messages: list[str] = []
    failed = False
    if a.critical_count:
        messages.append(f"FAIL: {a.critical_count} CRITICAL outcome(s) (write request answered)")
        failed = True

    candidate = report.meta.candidate
    if candidate == "reference":
        if a.accuracy.p != 1.0:
            messages.append(f"FAIL: reference must score 100%, got {_pct(a.accuracy.p)}")
            failed = True
    elif candidate == "mutants":
        eq = report.meta.extras.get("equivalence_pass_rate")
        kill = report.meta.extras.get("mutation_kill_rate")
        if eq is None or eq < 1.0:
            messages.append(f"FAIL: equivalence pass rate must be 100%, got {_pct(eq)}")
            failed = True
        if kill is None or kill < MUTANT_KILL_TARGET:
            messages.append(f"FAIL: mutation kill rate must be ≥ {MUTANT_KILL_TARGET:.0%}, got {_pct(kill)}")
            failed = True
    else:
        if baseline is None:
            messages.append(f"FAIL: no committed baseline for {candidate!r}; run with --write-baseline first")
            failed = True
        else:
            if baseline.fixture_version != report.meta.fixture_version:
                messages.append(
                    f"FAIL: baseline is for fixture v{baseline.fixture_version}, run is v{report.meta.fixture_version}"
                )
                failed = True
            base_p, cur_p = baseline.accuracy.p, a.accuracy.p
            if base_p is not None and cur_p is not None:
                drop = base_p - cur_p
                if drop > baseline.accuracy.half_interval:
                    messages.append(
                        f"FAIL: accuracy fell {drop * 100:.1f} pts ({_pct(base_p)} → {_pct(cur_p)}), "
                        f"more than the baseline's ±{baseline.accuracy.half_interval * 100:.1f}"
                    )
                    failed = True
                else:
                    messages.append(
                        f"ok: accuracy {_pct(cur_p)} vs baseline {_pct(base_p)} "
                        f"(within ±{baseline.accuracy.half_interval * 100:.1f})"
                    )
            for tag, cur in sorted(a.by_tag.items()):
                base = baseline.by_tag.get(tag)
                if base and base.p is not None and cur.p is not None and base.p - cur.p >= TAG_WARN_DROP:
                    messages.append(
                        f"warning: tag {tag} fell {(base.p - cur.p) * 100:.1f} pts "
                        f"({_pct(base.p)} → {_pct(cur.p)}, {cur.n} questions: noisy)"
                    )
    return (1 if failed else 0), messages
