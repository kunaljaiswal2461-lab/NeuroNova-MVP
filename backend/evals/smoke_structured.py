"""Five-minute smoke test of the strict JSON schemas against the real endpoint (SPEC-09 §T step 1).

    OPENAI_API_KEY=... STAGE4C_EVALUATION_ACK=disposable-synthetic-only python -m evals.smoke_structured

It makes exactly one pass-1 call and one pass-2 call against the fixture. A 400 from the API here (Part 5's open
risk: ``anyOf`` nullable objects in strict mode) means stop and report; do not start a recorded run. The offline part
(``check_strict``) runs everywhere and is part of the test suite.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from typing import Any

from evals import guards
from evals.cassette import ACK_ENV, ACK_VALUE

# Keywords OpenAI strict structured outputs do not accept on every model (Part 5 B5: limits live in Pydantic).
UNSUPPORTED_KEYWORDS = frozenset({
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "multipleOf", "uniqueItems", "minProperties", "maxProperties", "default", "examples",
})


def check_strict(schema: dict[str, Any], path: str = "$") -> list[str]:
    """Problems that strict mode would reject: every object closed and fully required, no unsupported keyword."""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: not an object"]
    for key in schema:
        if key in UNSUPPORTED_KEYWORDS:
            problems.append(f"{path}: unsupported keyword {key!r}")
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "object" in kinds:
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties must be false")
        if sorted(schema.get("required", [])) != sorted(properties):
            problems.append(f"{path}: every property must be required")
        for name, child in properties.items():
            problems += check_strict(child, f"{path}.{name}")
    if "array" in kinds and "items" in schema:
        problems += check_strict(schema["items"], f"{path}[]")
    for key in ("anyOf", "oneOf", "allOf"):
        for index, child in enumerate(schema.get(key, [])):
            problems += check_strict(child, f"{path}.{key}[{index}]")
    return problems


async def run() -> int:
    guards.assert_fixture_environment()
    if os.environ.get(ACK_ENV) != ACK_VALUE:
        print(f"set {ACK_ENV}={ACK_VALUE} (the fixture is synthetic data only)", file=sys.stderr)
        return 2
    from app.services.nl_sql import ENVELOPE_JSON_SCHEMA, answer_question
    from app.services.nl_sql.pass2 import write_presentation
    from app.services.nl_sql.pass2_spec import PASS2_JSON_SCHEMA
    from app.services import column_privacy
    from agentic_engine.llm_engine.openai_client import OpenAIClient
    from evals.bootstrap import bootstrap
    from evals.models import load_meta

    for name, schema in (("pass1_envelope", ENVELOPE_JSON_SCHEMA), ("pass2_spec", PASS2_JSON_SCHEMA)):
        problems = check_strict(schema)
        if problems:
            print(f"{name}: schema problems before any call: {problems}", file=sys.stderr)
            return 1
    meta = load_meta()
    fixture = await bootstrap()
    try:
        if not fixture.settings.openai_api_key:
            print("OPENAI_API_KEY is not set", file=sys.stderr)
            return 2
        llm = OpenAIClient(fixture.settings.openai_api_key)
        pipeline = await answer_question(
            session_factory=fixture.session_factory, settings=fixture.settings, llm=llm, user_id=fixture.user_id,
            source_id=fixture.source_id, question="How many orders have we received in total?",
            reference_time=dt.datetime.fromisoformat(meta.reference_clock), time_zone=meta.reference_time_zone,
            execute_kwargs={"adapter_factory": lambda _s: fixture.query_adapter_factory()},
        )
        print(f"pass 1: {pipeline.kind}" + (f" ({pipeline.failure_code})" if pipeline.failure_code else ""))
        if pipeline.kind != "answer":
            print("pass 1 did not produce an answer: stop and report (a 400 shows as llm_unavailable/envelope_invalid)")
            return 1
        async with fixture.session_factory() as session:
            flags = await column_privacy.load_effective_flags(session, fixture.source_id)
            await session.rollback()
        pass2 = await write_presentation(
            llm=llm, settings=fixture.settings, question="How many orders have we received in total?",
            envelope=pipeline.envelope, execution=pipeline.execution, flags=flags, consent=True, repaired=False,
        )
        print("pass 2: " + ("ok" if pass2.resolved is not None else f"fallback ({pass2.fallback})"))
        return 0 if pass2.resolved is not None else 1
    finally:
        await fixture.dispose()


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
