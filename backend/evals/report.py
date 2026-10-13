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
    # SPEC-08 B9; all optional so reports written before Part 5 still load.
    error_code: str | None = None
    intent_type: str | None = None
    notes: list[str] = Field(default_factory=list)
    # SPEC-09 W7. All optional so reports written before Part 9 still load.
    repair_reason: str | None = None
    pass2_ran: bool = False
    pass2_fallback: str | None = None
    pass2_display: str | None = None
    pass2_dropped: int = 0
    first_verdict: Verdict | None = None      # the verdict of the first attempt, before any repair
    chain: bool = False
    llm_calls: int = 0


class RepairReasonStats(_Model):
    attempted: int
    succeeded: int


class PipelineStats(_Model):
    """The SPEC-09 W7 report sections. Present only for runs that carry pipeline fields."""

    accuracy_without_repair: Accuracy | None = None
    repair_rate: float | None = None
    repair_success_rate: float | None = None
    repair_by_reason: dict[str, RepairReasonStats] = Field(default_factory=dict)
    display_accuracy: Accuracy | None = None
    pass2_runs: int = 0
    pass2_fallback_rate: float | None = None
    pass2_fallbacks: dict[str, int] = Field(default_factory=dict)
    number_check_drop_rate: float | None = None
    chain_accuracy: Accuracy | None = None
    single_turn_accuracy: Accuracy | None = None
    calls_per_answer: float | None = None
    input_tokens_per_answer: float | None = None
    output_tokens_per_answer: float | None = None
    cost_per_answer_usd: float | None = None
    personal_value_leaks: int = 0         # pass-2 prompts (consent on) that contained a personal fixture value


class IntentAccuracy(_Model):
    correct: int
    runs: int


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
    # SPEC-08 B9. Empty / zero for candidates that do not produce them.
    intent_accuracy: dict[str, IntentAccuracy] = Field(default_factory=dict)
    error_code_counts: dict[str, int] = Field(default_factory=dict)
    answers_lost_to_rejection: float | None = None
    clock_rewrites: int = 0
    pipeline: PipelineStats | None = None


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


def _rejection_codes() -> frozenset[str]:
    from app.services.sql_guard import RejectionCode

    return frozenset(c.value for c in RejectionCode)


def _intent_accuracy(records: list[RunRecord]) -> dict[str, IntentAccuracy]:
    """Share of answer-expected runs whose envelope ``output_intent.type`` equals ``expect.output_type``."""
    if not any(r.intent_type for r in records):         # a candidate that does not produce an intent
        return {}
    by_type: dict[str, list[RunRecord]] = defaultdict(list)
    for r in records:
        if r.expected_kind == "answer" and r.output_type:
            by_type[r.output_type].append(r)
    return {
        t: IntentAccuracy(correct=sum(r.intent_type == t for r in rs), runs=len(rs))
        for t, rs in sorted(by_type.items())
    }


def _pipeline_stats(records: list[RunRecord], pricing: dict[str, dict[str, float]]) -> PipelineStats | None:
    if not any(r.pass2_ran or r.repair_reason or r.first_verdict or r.chain for r in records):
        return None
    answers = [r for r in records if r.expected_kind == "answer"]
    stats = PipelineStats()
    with_first = [r for r in answers if r.first_verdict is not None]
    if with_first:
        stats.accuracy_without_repair = accuracy_of([r.model_copy(update={"verdict": r.first_verdict}) for r in with_first])
    repaired = [r for r in records if r.repair_reason]
    if records:
        stats.repair_rate = len(repaired) / len(records)
    if repaired:
        stats.repair_success_rate = sum(r.verdict in PASSING for r in repaired) / len(repaired)
        by_reason: dict[str, list[RunRecord]] = defaultdict(list)
        for r in repaired:
            by_reason[r.repair_reason or "?"].append(r)
        stats.repair_by_reason = {
            reason: RepairReasonStats(attempted=len(rs), succeeded=sum(r.verdict in PASSING for r in rs))
            for reason, rs in sorted(by_reason.items())
        }
    ran = [r for r in records if r.pass2_ran]
    stats.pass2_runs = len(ran)
    stats.personal_value_leaks = sum("pass2_personal_value_in_prompt" in r.notes for r in records)
    typed = [r for r in ran if r.output_type and r.expected_kind == "answer"]
    if typed:
        correct = sum(r.pass2_display == r.output_type for r in typed)
        stats.display_accuracy = Accuracy(
            p=correct / len(typed), n=len({r.question_id for r in typed}), runs=len(typed),
            half_interval=interval_half_width(correct / len(typed), len({r.question_id for r in typed})),
        )
    if ran:
        fallbacks = Counter(r.pass2_fallback for r in ran if r.pass2_fallback and not r.pass2_fallback.startswith("display_downgraded"))
        stats.pass2_fallbacks = dict(sorted(fallbacks.items()))
        stats.pass2_fallback_rate = sum(fallbacks.values()) / len(ran)
        stats.number_check_drop_rate = sum(r.pass2_dropped > 0 for r in ran) / len(ran)
    chains = [r for r in records if r.chain]
    singles = [r for r in records if not r.chain]
    if chains:
        stats.chain_accuracy = accuracy_of(chains)
        stats.single_turn_accuracy = accuracy_of(singles) if singles else None
    if answers:
        stats.calls_per_answer = sum(r.llm_calls for r in answers) / len(answers)
        stats.input_tokens_per_answer = sum(r.input_tokens for r in answers) / len(answers)
        stats.output_tokens_per_answer = sum(r.output_tokens for r in answers) / len(answers)
        cost = estimate_cost(answers, pricing)
        stats.cost_per_answer_usd = None if cost is None else cost / len(answers)
    return stats


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
    codes = Counter(r.error_code for r in records if r.error_code)
    expected_answers = [r for r in records if r.expected_kind == "answer"]
    rejections = _rejection_codes()
    lost = (
        sum(r.error_code in rejections for r in expected_answers) / len(expected_answers)
        if expected_answers and codes else None
    )
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
        intent_accuracy=_intent_accuracy(records),
        error_code_counts=dict(sorted(codes.items())),
        answers_lost_to_rejection=lost,
        clock_rewrites=sum("clock_literal" in r.notes for r in records),
        pipeline=_pipeline_stats(records, pricing if pricing is not None else load_pricing()),
    )


# ── markdown summary ─────────────────────────────────────────────────────────

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _pipeline_markdown(p: PipelineStats, final: Accuracy) -> list[str]:
    out = ["", "**Pipeline (SPEC-09)**", ""]
    if p.accuracy_without_repair is not None:
        out.append(
            f"- **Execution accuracy without repair → with repair:** {_pct(p.accuracy_without_repair.p)} → {_pct(final.p)}"
        )
    if p.repair_rate is not None:
        out.append(f"- **Repair rate:** {_pct(p.repair_rate)}; **repair success rate:** {_pct(p.repair_success_rate)}")
    if p.repair_by_reason:
        out += ["", "| Repair reason | Attempted | Succeeded |", "|---|---|---|"]
        out += [f"| `{r}` | {v.attempted} | {v.succeeded} |" for r, v in p.repair_by_reason.items()]
        out.append("")
    if p.display_accuracy is not None:
        out.append(f"- **Pass-2 display matches the expected output type:** {_pct(p.display_accuracy.p)} ({p.display_accuracy.runs} runs)")
    if p.pass2_runs:
        histogram = ", ".join(f"`{r}` {n}" for r, n in p.pass2_fallbacks.items()) or "none"
        out.append(f"- **Pass-2 fallback rate:** {_pct(p.pass2_fallback_rate)} of {p.pass2_runs} ({histogram})")
        out.append(f"- **Number-check drop rate (runs with a sentence removed):** {_pct(p.number_check_drop_rate)}")
    if p.pass2_runs:
        out.append(f"- **Personal values found in pass-2 prompts (consent on):** {p.personal_value_leaks}")
    if p.chain_accuracy is not None:
        out.append(
            f"- **Multi-turn accuracy:** {_pct(p.chain_accuracy.p)} ({p.chain_accuracy.n} chains) "
            f"vs single-turn {_pct(p.single_turn_accuracy.p if p.single_turn_accuracy else None)}"
        )
    if p.calls_per_answer is not None:
        cost = "" if p.cost_per_answer_usd is None else f", ≈ ${p.cost_per_answer_usd:.4f}"
        out.append(
            f"- **Per answer-expected run:** {p.calls_per_answer:.2f} model calls, "
            f"{p.input_tokens_per_answer:.0f} in / {p.output_tokens_per_answer:.0f} out tokens{cost}"
        )
    return out


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
    if a.error_code_counts:
        histogram = ", ".join(f"`{c}` {n}" for c, n in sorted(a.error_code_counts.items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"- **Error / rejection codes:** {histogram}")
        lines.append(f"- **Answers lost to validator rejections:** {_pct(a.answers_lost_to_rejection)}")
    if a.clock_rewrites:
        lines.append(f"- **Clock rewrites (now()/current_date made literal):** {a.clock_rewrites} runs")
    if a.intent_accuracy:
        lines += ["", "| Output type | Intent correct | Runs |", "|---|---|---|"]
        lines += [
            f"| {t} | {_pct(v.correct / v.runs if v.runs else None)} | {v.runs} |" for t, v in a.intent_accuracy.items()
        ]
    if a.pipeline is not None:
        lines += _pipeline_markdown(a.pipeline, acc)
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
