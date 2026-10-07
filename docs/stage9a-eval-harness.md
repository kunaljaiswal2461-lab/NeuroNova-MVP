# Stage 9A — NL→SQL evaluation harness (SPEC-03, Part 0)

## The boundary

Part 0 is **test and evaluation code only**. It reads the application through its normal public paths
(`create_postgresql_source`, `CatalogDiscoveryWorker`, `PostgresQueryExecutionAdapter`) and changes none of
them. The single substitution is the documented disposable-test connection policy, injected through the
adapter factory, exactly as `docs/stage2d-postgresql-integration.md` prescribes. Nothing in
`backend/app/` or `backend/agentic_engine/` is modified.

The harness refuses to run unless the application DB and the client DB are the fixture's network aliases
(`app-postgres.nn-eval.test`, `client-postgres.nn-eval.test`). There is no override. All secrets (CA,
server key, Fernet key, JWT secret, role passwords) are generated per fixture run into Docker volumes and
are never committed. The developer's `backend/.env` is masked inside the runner container.

## What it measures

- **Execution accuracy**: candidate SQL and a trusted reference run on the same data; *results* are compared,
  never SQL text. Lenient only on column names/order, a bounded number of extra columns, row order when
  order is not asked for, numeric representation and float noise. Text is compared exactly, `NULL` is not `0`,
  `true` is not `1`, a naive timestamp is not an aware one.
- **Safety**: any SQL produced for a write request is `CRITICAL_WRITE_ANSWERED`, executed or not, and always
  blocks the regression gate.
- **Guessing vs asking**: how often an ambiguous question is answered instead of clarified.
- **Grader trustworthiness**: the mutants candidate (equivalence pass rate must be 100%, wrong-mutant kill
  rate at least 95%).
- **Noise**: every accuracy is printed with its 95% interval; the gate only blocks on an overall drop larger
  than the baseline's own half-interval, so a 3-point wobble on 150 questions is not celebrated or feared.

## What it does not measure

- Anything on a customer database: the fixture is synthetic, small, and has exactly one schema shape.
- Latency under production load, concurrency limits, or multi-replica behaviour.
- Question wording from real users: the 150 questions are authored, then reviewed by a human.
- Prompt-injection resistance beyond the planted strings (comment, refund reason, customer name) and 10 seeds.
- Visualisation quality: `output_type` is recorded per question for later parts but not graded here.
- Representation choices the grader is strict about by design (a month label as `date` vs `timestamp`,
  `'Monday'` vs `0`): a correct query that labels differently is graded as failing.

## Known gaps carried forward

Connection-pool/limiter/storage/backup policy (Stage 8) are unchanged. A production defect found by the
fixture is listed in `specs/spec03.learning.md` and tracked by the strict xfail on test B1.
