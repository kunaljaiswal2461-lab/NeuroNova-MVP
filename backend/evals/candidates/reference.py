"""The reference candidate (SPEC-03 §13.2): runs each question's own reference SQL as nn_grader.

It must score 100%. If it does not, the grader or the harness is broken and nothing else can be
trusted. This is the harness's smoke test.
"""
from __future__ import annotations

import time

from evals import pgrun
from evals.candidates.base import CandidateOutcome, EvalContext
from evals.models import GoldenQuestion

MAX_ROWS = 1000


class ReferenceCandidate:
    name = "reference"
    requires_app = False

    async def answer(self, question: GoldenQuestion, ctx: EvalContext) -> CandidateOutcome:
        expect = question.expect
        if expect.kind == "clarification":
            return CandidateOutcome(kind="clarification", message="reference: this question is ambiguous")
        if expect.kind == "refusal":
            return CandidateOutcome(kind="refusal", message=f"reference: refused ({expect.category})")
        started = time.perf_counter()
        conn = await ctx.grader_connect()
        try:
            result = await pgrun.run_result(conn, expect.reference_sql, max_rows=MAX_ROWS)
        except Exception as exc:  # noqa: BLE001 - surface as a safe code, never the driver message
            return CandidateOutcome(kind="error", sql=expect.reference_sql, error_code=f"reference_failed:{type(exc).__name__}")
        finally:
            await conn.close()
        return CandidateOutcome(
            kind="answer",
            result=result,
            sql=expect.reference_sql,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
