"""Eval runner CLI (SPEC-03 §13.4).

    python -m evals.runner --suite golden --candidate reference --compare-baseline

Checks INV-P0-01 and the fixture version, runs the linter's static rules, builds the context,
runs every selected question *k* times, grades each run, writes a JSON report and prints a
markdown summary. Exit codes: 0 ok, 1 regression gate failed, 2 refused to run.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from evals import guards, lint, pgrun, report as rpt
from evals.candidates.base import Candidate, CandidateOutcome, EvalContext
from evals.candidates.mutants import MutantSummary, evaluate_question
from evals.candidates.reference import ReferenceCandidate
from evals.grader import ResultSet, grade
from evals.models import GOLDEN_DIR, MANIFEST_PATH, GoldenMeta, GoldenQuestion, load_meta, load_questions

REFERENCE_MAX_ROWS = 1000
MODEL_CANDIDATES = {"stage4c"}


class RunnerRefusal(Exception):
    """The runner will not start (version mismatch, lint errors, non-fixture host)."""


# ── preflight (B3) ───────────────────────────────────────────────────────────

def preflight(golden_dir: Path = GOLDEN_DIR, manifest_path: Path = MANIFEST_PATH) -> GoldenMeta:
    """Refuse on a fixture-version mismatch or on any static lint error."""
    meta = load_meta(golden_dir)
    manifest_version = json.loads(manifest_path.read_text())["fixture_version"]
    if meta.fixture_version != manifest_version:
        raise RunnerRefusal(
            f"golden set targets fixture v{meta.fixture_version} but the fixture is v{manifest_version}; "
            "re-run the linter, re-check reference answers and regenerate baselines"
        )
    errors, _ = lint.lint_static(golden_dir, manifest_path)
    if errors:
        shown = "\n".join(f"  {e}" for e in errors[:10])
        raise RunnerRefusal(f"{len(errors)} golden-set lint error(s):\n{shown}")
    return meta


# ── candidates ───────────────────────────────────────────────────────────────

def load_candidate(spec: str) -> Candidate:
    if spec == "reference":
        return ReferenceCandidate()
    if spec == "stage4c":
        from evals.candidates.stage4c import Stage4cCandidate

        return Stage4cCandidate()
    if spec == "nl_sql":
        from evals.candidates.nl_sql import NlSqlCandidate

        return NlSqlCandidate()
    if spec == "nl_sql_full":
        from evals.candidates.nl_sql_full import NlSqlFullCandidate

        return NlSqlFullCandidate()
    if ":" in spec:
        module_name, _, class_name = spec.partition(":")
        return getattr(importlib.import_module(module_name), class_name)()
    raise RunnerRefusal(f"unknown candidate {spec!r}: use reference | mutants | stage4c | nl_sql | nl_sql_full | <dotted.path:Class>")


# ── running ──────────────────────────────────────────────────────────────────

def select_questions(suite: str, tags: set[str] | None) -> list[GoldenQuestion]:
    questions = load_questions(suite)
    if tags:
        questions = [q for q in questions if tags & set(q.tags)]
    return questions


async def reference_results(
    questions: list[GoldenQuestion], grader_connect
) -> tuple[dict[str, ResultSet], dict[str, list[ResultSet]]]:
    """Trusted answers (and acceptable alternatives) for every question, run once as nn_grader."""
    refs: dict[str, ResultSet] = {}
    alts: dict[str, list[ResultSet]] = {}
    conn = await grader_connect()
    try:
        for q in questions:
            if q.expect.reference_sql:
                refs[q.id] = await pgrun.run_result(conn, q.expect.reference_sql, max_rows=REFERENCE_MAX_ROWS)
            if q.expect.acceptable_answers:
                alts[q.id] = [
                    await pgrun.run_result(conn, a.reference_sql, max_rows=REFERENCE_MAX_ROWS)
                    for a in q.expect.acceptable_answers
                ]
    finally:
        await conn.close()
    return refs, alts


def _record(
    q: GoldenQuestion, run_index: int, outcome: CandidateOutcome, g, first_verdict=None
) -> rpt.RunRecord:
    presentation = outcome.presentation or {}
    return rpt.RunRecord(
        repair_reason=outcome.repair_reason,
        pass2_ran=outcome.pass2_ran,
        pass2_fallback=outcome.pass2_fallback,
        pass2_display=presentation.get("display"),
        pass2_dropped=outcome.pass2_dropped,
        first_verdict=first_verdict,
        chain=bool(q.turns),
        llm_calls=len(outcome.llm_calls),
        question_id=q.id,
        tags=list(q.tags),
        output_type=q.expect.output_type,
        expected_kind=q.expect.kind,
        run_index=run_index,
        verdict=g.verdict,
        reason=g.reason,
        sql=outcome.sql,
        latency_ms=outcome.latency_ms,
        error_code=outcome.error_code,
        intent_type=(outcome.output_intent or {}).get("type"),
        notes=list(outcome.notes),
        input_tokens=sum(c.input_tokens for c in outcome.llm_calls),
        output_tokens=sum(c.output_tokens for c in outcome.llm_calls),
        models=[c.model for c in outcome.llm_calls],
        examples=[tuple(e) for e in g.examples],
    )


async def evaluate(
    questions: list[GoldenQuestion],
    candidate: Candidate,
    ctx: EvalContext,
    *,
    repeat: int,
    concurrency: int,
    refs: dict[str, ResultSet],
    alts: dict[str, list[ResultSet]],
) -> list[rpt.RunRecord]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(q: GoldenQuestion, i: int) -> rpt.RunRecord:
        async with semaphore:
            try:
                outcome = await candidate.answer(q, replace(ctx, extras={**ctx.extras, "run": i}))
            except Exception as exc:  # noqa: BLE001 - a crashing candidate is a failed run, not a crashed harness
                outcome = CandidateOutcome(kind="error", error_code=f"candidate_exception:{type(exc).__name__}")
        g = grade(q.expect, refs.get(q.id), outcome, alts.get(q.id, ()))
        first = None
        if outcome.first_attempt is not None:
            first = grade(q.expect, refs.get(q.id), outcome.first_attempt, alts.get(q.id, ())).verdict
        return _record(q, i, outcome, g, first)

    jobs = [one(q, i) for q in questions for i in range(repeat)]
    return list(await asyncio.gather(*jobs))


async def evaluate_mutants(
    questions: list[GoldenQuestion], refs: dict[str, ResultSet], grader_connect
) -> tuple[list[rpt.RunRecord], MutantSummary]:
    summary = MutantSummary()
    conn = await grader_connect()
    try:
        async def run(sql: str) -> ResultSet:
            return await pgrun.run_result(conn, sql, max_rows=REFERENCE_MAX_ROWS)

        for q in questions:
            if q.expect.kind == "answer":
                summary.results.extend(await evaluate_question(q, refs[q.id], run))
    finally:
        await conn.close()
    records = []
    for r in summary.results:
        if r.group != "equivalent" or r.status == "invalid":
            continue
        from evals.grader import Verdict

        records.append(
            rpt.RunRecord(
                question_id=f"{r.question_id}::{r.kind}", tags=list(r.tags), expected_kind="answer", run_index=0,
                verdict=Verdict(r.verdict), reason=r.detail, sql=r.sql,
            )
        )
    return records, summary


def _git_commit() -> str | None:
    if os.environ.get("NN_GIT_COMMIT"):
        return os.environ["NN_GIT_COMMIT"]
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mutant_markdown(summary: MutantSummary) -> str:
    x = summary.extras()
    lines = [
        "",
        f"- **Equivalence pass rate:** {rpt._pct(x['equivalence_pass_rate'])} "
        f"({x['equivalence_total'] - x['equivalence_failed']}/{x['equivalence_total']})",
        f"- **Mutation kill rate:** {rpt._pct(x['mutation_kill_rate'])} "
        f"({x['wrong_killed']} killed, {x['survivors']} survivors, {x['accepted_survivors']} accepted)",
        f"- **Survivors:** {x['survivors_data_insensitive']} data-insensitive, {x['survivors_grader_lenient']} grader-lenient",
        f"- **Invalid mutants (excluded):** {x['invalid_mutants']}",
    ]
    for r in summary.survivors:
        lines.append(f"  - survivor `{r.question_id}` {r.kind} [{r.survivor_class}]: {r.detail}")
    for r in summary.results:
        if r.group == "equivalent" and r.status == "fail":
            lines.append(f"  - EQUIVALENT REJECTED `{r.question_id}` {r.kind}: {r.verdict}")
        if r.status == "invalid":
            lines.append(f"  - invalid `{r.question_id}` {r.kind} ({r.verdict})")
    return "\n".join(lines)


async def run_cli(args: argparse.Namespace) -> int:
    guards.assert_fixture_environment()
    try:
        meta = preflight()
    except RunnerRefusal as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2

    tags = set(args.tags.split(",")) if args.tags else None
    questions = select_questions(args.suite, tags)
    if not questions:
        print("no questions selected", file=sys.stderr)
        return 2

    started = _now()
    is_mutants = args.candidate == "mutants"
    candidate = None if is_mutants else load_candidate(args.candidate)
    uses_model = args.candidate in MODEL_CANDIDATES or getattr(candidate, "uses_model", False)
    cassette_mode = args.cassette or ("replay" if uses_model else "off")
    repeat = args.repeat or (3 if uses_model else 1)
    candidate_name = "mutants" if is_mutants else getattr(candidate, "name", args.candidate)
    try:
        extras_in = dict(item.split("=", 1) for item in (args.extra or []))
    except ValueError:
        print("--extra needs key=value", file=sys.stderr)
        return 2

    refs, alts = await reference_results(questions, pgrun.connect_grader)
    extras: dict = {}
    fixture = None
    try:
        if is_mutants:
            records, summary = await evaluate_mutants(questions, refs, pgrun.connect_grader)
            extras = summary.extras()
            repeat = 1
        else:
            if getattr(candidate, "requires_app", False):
                from evals.bootstrap import bootstrap

                fixture = await bootstrap()
            extras = dict(extras_in)            # recorded in the report so "samples=off" is visible in the JSON
            ctx = EvalContext(
                fixture=fixture,
                grader_connect=pgrun.connect_grader,
                reference_clock=meta.reference_clock,
                reference_time_zone=meta.reference_time_zone,
                cassette_mode=cassette_mode,
                extras=extras_in,
            )
            records = await evaluate(
                questions, candidate, ctx, repeat=repeat, concurrency=args.concurrency, refs=refs, alts=alts
            )
    finally:
        if fixture is not None:
            await fixture.dispose()

    run_report = rpt.Report(
        meta=rpt.RunMeta(
            git_commit=_git_commit(),
            fixture_version=meta.fixture_version,
            candidate=candidate_name,
            suite=args.suite,
            models=sorted({m for r in records for m in r.models}),
            repeat=repeat,
            started_at=started,
            finished_at=_now(),
            extras=extras,
        ),
        aggregates=rpt.aggregate(records),
        records=records,
    )
    path = rpt.write_report(run_report)
    print(rpt.render_markdown(run_report))
    if is_mutants:
        print(_mutant_markdown(summary))
    print(f"\nreport: {path}")

    code = 0
    if args.compare_baseline:
        code, messages = rpt.compare_baseline(run_report, rpt.load_baseline(candidate_name))
        print()
        for m in messages:
            print(m)
        print("regression gate:", "FAILED" if code else "passed")
    if args.write_baseline:
        print("baseline written:", rpt.write_baseline(rpt.baseline_from_report(run_report)))
    return code


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m evals.runner")
    ap.add_argument("--suite", choices=["golden", "adversarial", "chains", "all"], default="golden")
    ap.add_argument("--candidate", default="reference", help="reference | mutants | stage4c | nl_sql | nl_sql_full | <dotted.path:Class>")
    ap.add_argument("--tags", help="comma-separated tag filter")
    ap.add_argument("--repeat", type=int, help="runs per question (default 1; 3 for model candidates)")
    ap.add_argument("--cassette", choices=["off", "record", "replay"])
    ap.add_argument("--extra", action="append", metavar="KEY=VALUE", help="candidate variant, e.g. samples=off")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--compare-baseline", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run_cli(args))
    except (RunnerRefusal, guards.FixtureGuardError) as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
