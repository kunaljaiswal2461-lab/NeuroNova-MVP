# NL→SQL evaluation harness (SPEC-03, Part 0)

Measures whether generated SQL is **right** (execution accuracy against reviewed reference answers) and
**safe** (no write request is ever answered), on a realistic, hostile, deterministic Postgres fixture.
Part 0 adds test/eval code only; nothing under `backend/app/` or `backend/agentic_engine/` is modified.

## Run it

```bash
# Layer 1: anywhere, no Docker / network / model key (this is what plain pytest does)
cd backend && pytest -q

# Layer 2: the fixture (run from the repo root)
docker compose -f backend/evals/fixture/compose.yml up -d --build
docker compose -f backend/evals/fixture/compose.yml run --rm runner sh -c "
  NN_LIVE_PG=1 pytest -q -m live_pg &&
  python -m evals.lint --live &&
  python -m evals.runner --suite golden --candidate reference --compare-baseline &&
  python -m evals.runner --suite golden --candidate mutants   --compare-baseline"
docker compose -f backend/evals/fixture/compose.yml down -v

# Rebuild-from-scratch reproducibility check (F9; needs the Docker socket)
sh backend/evals/fixture/verify_rebuild.sh
```

Poke at the fixture: `docker compose -f backend/evals/fixture/compose.yml exec client-postgres psql -U postgres -d nn_client`.
No service publishes a port to the host. `down -v` gives you a fresh CA, fresh passwords and fresh data on the next `up`.

## Pieces

| Path | What it is |
|---|---|
| `grader.py` | Pure result comparison: column mapping, tolerance, NULL/case/zone rules (§12) |
| `lint.py` | Golden-set linter. Static rules L1–L7, live rules LL1–LL5 plus LL6 (ties inside the result) |
| `golden/` | `meta.yaml`, 150 questions in `questions/`, 10 seeds in `adversarial/` |
| `candidates/reference.py` | Runs each reference SQL; must score 100% |
| `candidates/mutants.py` | Grader self-test: equivalent rewrites must pass, wrong mutations must fail |
| `candidates/stage4c.py` | Optional baseline of today's typed-plan pipeline (needs a key + ACK) |
| `runner.py` / `report.py` | CLI, JSON + markdown reports, baselines, regression gate |
| `cassette.py` | Record/replay LLM client for later parts |
| `generator/` | Deterministic data (`random.random()` only), anchors computed in Python |
| `fixture/` | Compose topology, TLS, roles, schema, grants, 2M-row table |
| `bootstrap.py`, `guards.py`, `pgrun.py`, `checksums.py` | App-side bootstrap, host guard (INV-P0-01), asyncpg helpers, manifest |

## Exit codes of `python -m evals.runner`

`0` fine · `1` regression gate failed (`--compare-baseline`) · `2` refused to run (non-fixture host, fixture
version mismatch, golden-set lint errors).

## Changing the fixture

Any change to the data or schema bumps `fixture_version` in `fixture/manifest.json` **and**
`golden/meta.yaml`, then: regenerate the manifest (`python -m evals.checksums --write` inside the runner),
re-run the linter, re-check reference answers, and regenerate baselines.

### Planner statistics (SPEC-05 B7)

`fixture/client/99_analyze.sql` runs `ANALYZE;` last in the init order. Part 2's cost guard compares
`EXPLAIN` total cost with a ceiling, and the estimate is only meaningful with statistics on every table
(without them the planner guesses 1,000-row defaults and the number says nothing). Any new init file that
loads data must sort before `99_`.

### The `drift` schema and `nn_drift` role (SPEC-05 L2 item 6)

`fixture/client/40_drift.sql` creates a scratch schema `drift` owned by a login role `nn_drift` (no
superuser, no rights outside `drift`; `pg_hba.conf` allows it over TLS like the other roles, its password is
a per-run secret like theirs). The drift tests create a table there, build a throwaway source over it, then
drop a column, revoke `SELECT`, enable RLS, swap in a view or change the owner, and expect the executor to
refuse. Nothing in `shop` is touched, so the manifest checksums are unchanged. An existing fixture needs
`docker compose ... down -v` and `up -d --build` once to pick the role up.

## Accepting a mutation survivor

A wrong mutant that still grades PASS is a *survivor*. Either strengthen the data/question, or put
`accept-survivor:<kind> (reason)` in the question's `notes`. Accepted survivors are reported separately and
excluded from the kill rate, never silently.
