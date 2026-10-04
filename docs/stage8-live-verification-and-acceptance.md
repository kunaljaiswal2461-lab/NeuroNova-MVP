# Stage 8 — Live Verification and Acceptance

This report records the verification that was actually performed on
2026-10-04, before this report was committed. It covers the accumulated Stage
6B and Stage 7 work and does not treat mock, SQLite, static SQL, or
browser-unit evidence as a substitute for a live PostgreSQL/TLS/model/browser
check.

## Tested environment

- Repository working branch at the time of verification:
  `stage6a-optional-database-profiling`; migration head:
  `0015_stage7_source_operations`. This is historical test provenance, not a
  claim about the branch where this report is subsequently stored.
- Application-metadata verification: a user-owned local PostgreSQL 18.4
  server, using three explicitly named disposable databases. Only synthetic
  application records were seeded; all three databases were dropped after the
  checks. No client database, credential, or production resource was used.
- Default local runtime: SQLite development configuration, connectors disabled,
  no connector egress acknowledgement, and no model API key.
- Docker is available locally, but no approved Stage 2D TLS/restricted-role
  fixture, supervised worker topology, running API/frontend, or authenticated
  browser session exists. The checked-in Compose file is not such a fixture.

## Acceptance matrix

| Requirement and fixture | Actual result and evidence | Status |
| --- | --- | --- |
| Repository baseline and migration graph | `alembic heads` reported the single head `0015_stage7_source_operations`; the Stage 7 documents and deployment configuration were inspected. | PASS — repository evidence |
| Fresh metadata-database upgrade | An empty disposable PostgreSQL 18.4 database upgraded from base to `0015`. The resulting `alembic_version.version_num` length was 255 and all eight Stage 7 columns were present. | PASS — actual PostgreSQL |
| Upgrade from the pre-Stage-7 head | A second disposable database was upgraded to `0014_optional_source_profiling`, seeded with one synthetic credential, scope, catalog/run/snapshot/head/asset/column, metric/version, profile run, source-bound session, and typed chat result, then upgraded to `0015`. Every seeded dependent-table count remained one; the typed result was retained before any scope change. | PASS — actual PostgreSQL |
| Stage 7 defaults and constraint | Post-upgrade source state was generation `0`, refresh disabled with no due/success/failure values; run state was generation `0`, not scheduled. A transaction attempting negative generation was rejected and rolled back. | PASS — actual PostgreSQL |
| Source deletion cascades | A transaction deleted the single synthetic source. All checked source children — credential, scope, runs, snapshots, heads, catalog assets/columns, definitions/versions, profiles, source-bound sessions, and chat messages — fell to zero, then the transaction was rolled back. | PASS — actual PostgreSQL |
| Documented Stage 7 rollback/re-upgrade | `0015 → 0014` removed all eight Stage 7 columns while preserving the synthetic predecessor records. Re-upgrading to head restored the columns and preserved the typed row. | PASS — actual PostgreSQL |
| Alembic revision-bookkeeping rollback | On a third empty database, `0011b_widen_alembic_version` widened the version column from 32 to 255; its downgrade restored 32 at `0011_postgres_chat_bindings`; re-upgrade reached `0015`. | PASS — actual PostgreSQL |
| Known-answer single-table, metric, and join flows | No separate synthetic client PostgreSQL fixture with TLS, a restricted reader role, and a published catalog was available. No numeric answer, join grain, provenance, or rejection was represented as live evidence. | BLOCKED — missing isolated client/TLS fixture |
| Lifecycle retest, rotation, scope, and stale remote work | Backend contract coverage exists and a deterministic post-executor scope-change regression now passes, but no live TLS client connection or cross-process running work was available. | BLOCKED — live client/work topology missing |
| Typed-result disclosure after scope/head change | Deterministic barriers change either generation/scope or the current catalog head after the executor returns but before delivery. The typed result is neither persisted nor emitted. The production path locks the source row, rechecks the pinned head, and holds the lock through the row-bearing SSE yield. | PASS — targeted contract/regression evidence; remote execution remains unverified |
| Scheduling, concurrent workers, leases, restart, and liveness | No separately supervised `catalog_discovery_worker`, multi-worker fixture, or restart supervisor was running. Existing contract tests do not prove process recovery. | BLOCKED — worker/supervisor topology missing |
| TLS, ACL/RLS, restricted role, timeout, cancellation, DDL lock, and result limits | No test CA/server certificate, private DNS fixture, restricted client role, or synthetic client database was available. | BLOCKED — security execution fixture missing |
| Real opt-in profiling and charts | Frontend unit tests/build passed, but no connected synthetic table was profiled with `TABLESAMPLE SYSTEM`; no live chart data or cancellation behavior was observed. | BLOCKED — client fixture missing |
| Actual model behavior and one-repair behavior | Model credentials and `STAGE4C_EVALUATION_ACK=disposable-synthetic-only` were absent. Malformed-envelope behavior is contract-tested only. | BLOCKED — explicit acknowledgement and non-production model key missing |
| Saved-source UI contract | Focused jsdom coverage retests only the saved source, exposes names-only scope choices, sends JWT-only requests, and clears replacement username/password/CA form state after a successful rotation. This is not a rendered authenticated browser run. | PASS — focused UI contract evidence |
| Authenticated desktop/narrow browser flows | No local API/frontend was running, no authenticated test account/session existed, and the available desktop automation surface had no controllable browser due to pending macOS permissions. | BLOCKED — running topology, test account, and browser permission missing |
| Storage persistence, backup/restore, retention, and durable volume | No approved durable-volume/object-store or backup policy/topology was present. The existing local-storage limitations remain unchanged. | BLOCKED — deployment policy/topology missing |
| Automated regression suites | Backend `pytest -q`: 393 passed. Frontend `npm run test:ui`: 115 passed. Frontend production build and scoped lint passed. | PASS — contract/build evidence |

## Defects found and fixed

### Fresh PostgreSQL migrations stopped at revision `0012`

Alembic creates `alembic_version.version_num` as `VARCHAR(32)`, while the
existing `0012_confirmed_metric_definitions` identifier is 33 characters. A
fresh PostgreSQL upgrade therefore completed that revision's DDL but failed
while recording the revision.

`0011b_widen_alembic_version` now runs immediately after the short `0011`
identifier and widens only Alembic's bookkeeping column to `VARCHAR(255)`.
`0012` now follows that bridge. The bridge has a tested reverse path and a
small migration-graph regression test protects the ordering.

### Scope change could race final source-chat result delivery

The Stage 4B executor revalidated a source before returning, but the chat
layer could previously persist and stream rows after a scope or same-scope
catalog-head change committed in the gap after that return. The fix adds an
excluded internal delivery binding containing only owner/source/snapshot,
configuration generation, and required relation identities. The chat layer
validates it and the current catalog head under the same source-row lock used
by maintenance mutations and catalog publication, uses that lock for the
row-bearing transcript write, and holds it through the row-bearing SSE yield.
The binding is excluded from typed result serialization, persisted JSON,
prompts, and SSE payloads.

The regressions use synchronization events rather than timing: they change
the synthetic source generation/scope or replace the same-scope catalog head
after execution returns, then prove the result never reaches transcript
persistence or `POSTGRES_RESULT`.

## Validation commands

```bash
cd backend
pytest -q                              # 393 passed
python3 -m compileall -q app agentic_engine
alembic heads                          # 0015_stage7_source_operations

cd ../neuronova-frontend
npm run test:ui                        # 115 passed
npm run build
npx eslint src/api/sources.js src/components/sources/DatabaseSourcesPanel.jsx \
  src/components/sources/SavedSourceMaintenance.jsx \
  src/components/sources/SourceProfilingPanel.jsx \
  src/components/charts/DatabaseChartRenderer.jsx \
  src/components/charts/databaseChartSpec.js src/pages/DatabaseChat.jsx \
  tests/databaseChat.test.jsx tests/sourceProfiling.test.jsx \
  tests/databaseChartSpec.test.js tests/savedSourceMaintenance.test.jsx
```

`git diff --check` also passed. The PostgreSQL migration results in this
report came from real upgrades/downgrades, not `--sql` output.

## Remaining decisions and prerequisites

The overall documented deployment topology is **not accepted** and this work
does **not** establish production readiness. To complete the blocked cases,
an operator needs to provide or explicitly authorize an isolated synthetic
topology with:

1. separate fresh and upgrade application databases plus a distinct synthetic
   client PostgreSQL database;
2. a private DNS name covered by a generated test CA/server certificate and a
   restricted non-owner reader role;
3. ephemeral connector encryption material, connector activation, and egress
   acknowledgement supplied only to the fixture process;
4. a separately supervised web process, discovery worker, and restart/liveness
   environment;
5. an explicit Stage 4C evaluation acknowledgement and non-production model
   key for actual-model checks; and
6. a running authenticated browser test environment with completed desktop
   automation permissions, plus an approved storage/backup/retention policy.

For the narrow tested topology, the application-metadata migration paths,
synthetic preservation/cascade checks, and the new delivery-race regression
are accepted. All client-database, model, browser, worker-liveness, and
storage claims remain deliberately blocked rather than inferred.
