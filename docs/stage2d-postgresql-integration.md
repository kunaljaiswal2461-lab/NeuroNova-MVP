# Stage 2D PostgreSQL onboarding integration procedure

This procedure verifies the Stage 2B/2C onboarding path against disposable
PostgreSQL services. It is intentionally separate from the application's
normal Compose database: never point `DATABASE_URL` at the client fixture and
never use a customer database, customer credential, or production encryption
key.

The checked-in `docker-compose.yml` does **not** meet this procedure. It has
one application PostgreSQL service and no distinct restricted client role or
TLS fixture. It must not be treated as evidence of the checks below.

## Required isolated topology

Create an ephemeral test project with the following resources on one private
test network:

| Resource | Purpose | Isolation requirement |
| --- | --- | --- |
| `stage2d-app-fresh` | fresh NeuroNova application database | migrated directly from an empty database to `head` |
| `stage2d-app-upgrade` | upgrade-path NeuroNova application database | migrated to `0006_chat_message_unsupported`, seeded, then migrated to `head` |
| `stage2d-client` | customer-like PostgreSQL database | never receives NeuroNova migrations or application tables |
| `stage2d-client-reader` | client database role | only `CONNECT`, selected-schema `USAGE`, and explicit table-level `SELECT` |
| test CA and server certificate | client service TLS | certificate SAN contains `client-postgres.stage2d.test`; client trusts only the generated test CA |

Give the client database a DNS name matching the certificate, such as
`client-postgres.stage2d.test`, and expose PostgreSQL on port 5432 *inside the
private test network*. Do not solve local connectivity by weakening the normal
public-DNS policy, disabling certificate verification, or changing application
settings. In the integration fixture only, inject
`PostgresConnectionPolicy.for_disposable_test(resolver)` into
`PostgresOnboardingAdapter`, with a resolver that returns the private fixture
address. The source-service default factory is intentionally resolved at call
time so a same-process integration fixture can monkeypatch
`app.services.source_service._build_adapter` before issuing an HTTP request
with that explicit adapter factory. No request field, environment variable, or
application setting can select the test policy. That explicit constructor is
the only permitted test exception.

Generate a Fernet key in the test harness for each run and pass it only through
the test process environment. Do not put it, database passwords, CA private
keys, or certificate material in this repository or command output.

## Client fixture

Use synthetic names and rows that contain no sensitive values. Prepare these
assets in a non-system schema:

- An allowed base table with complete table-level `SELECT`.
- A base table with no `SELECT`.
- A base table in a schema for which the role has no `USAGE`.
- A table having only column-level grants.
- A view and a materialized view.
- A base table created **after** a source scope has been saved.

The restricted role must have no write, DDL, role-management, replication, or
`pg_read_all_data` privilege. Capture PostgreSQL statement logging only if it
can be confined to the disposable service, and assert that onboarding executes
the names-only `information_schema` inventory query: no fixture table rows are
read or returned.

## Migration and persistence checks

Run backend Alembic commands from `backend/` against the two application
databases, using separately supplied disposable database URLs:

1. Upgrade `stage2d-app-fresh` from empty to `head`.
2. Upgrade `stage2d-app-upgrade` to `0006_chat_message_unsupported`; seed one
   user, dataset, conversation, and existing chat record through the
   application's normal model/session layer; then upgrade it to `head`.
3. Verify the seed records remain present. Verify `data_sources.owner_user_id`
   is restrictive, a source has one credential record, scope rows are unique
   per `(source_id, schema_name, table_name)`, deleting a source cascades its
   credential and scope rows, and normal account deletion removes owned
   sources before the user.

Do not count `alembic upgrade --sql`, SQLite tests, or a manually inspected
migration as PostgreSQL migration verification.

## Stage 3A catalog-persistence checks

Stage 3A adds only application-database catalog records; it must not trigger
client database discovery. Before testing an upgrade, inspect the target
application database's `source_status_enum`. If it contains `CATALOG_READY`
from the earlier, unreleased form of `0007_postgres_sources`, stop: a separate
forward compatibility migration is required before this branch can be used.
Editing the historical `0007` file cannot repair an already-applied enum.

The catalog run lifecycle is `QUEUED` → `RUNNING` → `SUCCEEDED`, with
`FAILED` or `CANCELLED` available from either non-terminal state. Terminal
states do not transition. A stale generation is cancelled and a changed scope
fails the run. The unique `catalog_snapshots.sync_run_id` is the successful
run's published-snapshot reference; publication moves the separate
source-current-head pointer only after the complete immutable snapshot commits.

Using synthetic application-db users, sources, and explicit scope rows only:

1. Upgrade a fresh application database and a database already at the current
   `0007_postgres_sources` revision to `0008_catalog_persistence`. Inspect the
   native enum, composite foreign-key, unique-index, and check-constraint DDL.
2. Call the internal catalog service with normalized fixture metadata, without
   invoking the connector. Verify a queued run becomes running, then publishes
   a complete snapshot containing comments, ordered native columns, composite
   keys, and composite foreign-key mappings. Verify dotted, quoted, and colon
   identifier parts remain separate schema/table/column values.
3. Attempt direct cross-source and cross-snapshot inserts for a catalog asset,
   key mapping, relationship, and current-head pointer. Each must fail at the
   database boundary. Verify an out-of-scope relationship has only its local
   constraint identity and omission status; it must not persist excluded target
   table or column details.
4. With two application-db sessions and a synchronization barrier, queue two
   runs for one source. Publish the newer generation first and prove the older
   run cannot replace the current head. Verify `SELECT ... FOR UPDATE` and
   transaction isolation, not merely a process-local test lock, provide this
   behavior. In a separate barrier case, try to add or delete a selected scope
   row while a run captures or rechecks scope; source and scope-row locks must
   prevent a current head from being published for a stale scope. Future scope
   mutation code must acquire the source lock before any scope-row lock.
5. Inject a fixture constraint failure while publishing after an earlier
   snapshot exists. Confirm every row for the new snapshot rolls back, the old
   current head remains, and a cancelled or terminal run cannot publish.
6. Delete a source directly, then separately use normal account deletion.
   Confirm every catalog run, snapshot, head, asset, column, key, and
   relationship child is removed by database cascades.

Record only synthetic identifiers, revision names, counts, and safe failure
codes. These checks verify application-database catalog persistence only; they
do not replace the Stage 2D client TLS, ACL, or onboarding checks.

## Stage 3B durable-discovery checks

Stage 3B extends the application database from `0008_catalog_persistence` to
`0009_catalog_discovery_worker`; it does not change client data. Execute these
checks only after the Stage 3A checks and with the isolated application/client
topology above:

1. Upgrade a fresh application database and a database at `0008` to `0009`.
   Inspect the lease, attempt, cancellation, retry, and claim-index DDL. Then
   run a downgrade to `0008` on a disposable copy and upgrade it to `0009`
   again. Do not treat offline SQL generation as this verification.
2. Start two separately configured discovery worker processes. Use a barrier
   around their claims of queued requests for two sources and prove
   `FOR UPDATE ... SKIP LOCKED` gives one lease per source and does not hold an
   application transaction through client discovery. Record only lease state,
   not tokens.
3. Kill one worker after it begins a deliberately delayed synthetic discovery.
   After lease expiry, prove another worker recovers the request and that the
   first worker's publication, renewal, and failure finalization are all
   rejected by the token fence. Verify bounded retry and terminal failure
   behavior separately.
4. Request cancellation before discovery and during a delayed discovery. In
   both cases verify no new snapshot publishes; in the first case the client
   adapter must not be called. Delete a source and change an explicit scope
   after a claim but before publication and prove the former cascades work and
   the latter fails the run while retaining the prior head.
5. With the restricted TLS client role, discover a selected fixture that has
   comments, multi-column primary/unique keys, a composite foreign key to an
   included table, and a foreign key to an excluded table. Verify ordered
   native metadata persists through the Stage 3A models; the excluded target's
   names and columns never persist. Use statement logging confined to the
   disposable client service to prove no client relation row is selected.
6. Revoke selected-table or column access, remove a selected table, deny the
   metadata read, and exceed each discovery size/time limit. Each must return
   a safe terminal code, close the connection, and preserve the earlier head;
   none may publish a structurally incomplete snapshot.
7. With two JWT users, exercise all four catalog-sync APIs. The owner may
   request, inspect, summarize, and cancel; the other user receives a
   non-enumerating `404`, and missing/invalid/API-key-only credentials receive
   `401`. Check no response exposes a lease token, ciphertext, metadata
   comment, client row, or raw driver error.

This live procedure remains pending until it is actually executed against the
isolated PostgreSQL/TLS fixture. SQLite and mocked-adapter tests only cover
the application and adapter contracts, not PostgreSQL lock, migration, or
privilege semantics.

## Stage 4B controlled-execution checks

Run these only against the isolated topology above, after the Stage 3B
procedure succeeds. Invoke the internal `execute_postgres_query(...)` gateway
from a fixture-owned application process; Stage 4B intentionally has no HTTP
or arbitrary-SQL endpoint. Use synthetic rows and discard the disposable
project after the run.

1. Upgrade one fresh application database from empty to `0010` and a second
   disposable copy from `0009_catalog_discovery_worker` to `0010`. Confirm
   `postgres_query_runs` has only the redacted provenance columns and its
   owner/source/snapshot/asset composite foreign keys. Do not treat offline
   SQL output as this check.
2. Publish a current catalog snapshot for one allowed ordinary base table.
   Execute compiler-produced plans covering typed filters (including nulls),
   `count(*)`/`count(column)`, sum/average/min/max, grouping/date buckets,
   ordering, and requested limits. Compare bounded typed values with known
   synthetic answers, including Decimal, large integers, UUIDs, dates, and
   timestamps. Confirm an empty result is successful.
3. Capture server-side session state during each fixture query. Confirm a
   dedicated verified-TLS connection uses the restricted reader, a
   read-only `READ COMMITTED` transaction, `search_path = pg_catalog`, UTC,
   application name, and configured statement/lock/idle/operation limits.
   Attempt an insert/DDL from that role and prove it fails. Do not weaken TLS,
   DNS, role grants, or destination policy to make this pass.
4. Exercise the relation gate with a view, materialized view, foreign table,
   partition root/leaf, inheritance parent/child, RLS-enabled table,
   table-owner role, superuser, and `BYPASSRLS` role. Each unsupported case
   must return only a safe failure code and no rows. Verify schema/table and
   column privilege revocation separately.
5. Coordinate a concurrent incompatible DDL attempt after the gateway takes
   its `ACCESS SHARE` lock. Prove it blocks until completion, then repeat with
   a pre-lock same-name replacement and record the documented catalog-lineage
   limitation. Verify incompatible output/column type changes fail safely.
6. Use a deliberately slow fixture statement and a cancellation proxy. Verify
   statement/operation timeout and both internal/caller cancellation terminate
   the dedicated client connection, release concurrency slots, write no retry
   attempt, and expose no partial rows. Repeat with row and serialized-byte
   caps, including an oversized individual cell.
7. Revoke table scope, deactivate the source, and remove owner access after
   remote execution begins but before result delivery. The gateway must
   withhold all fetched rows and finalize only redacted provenance. Inject a
   terminal provenance-write failure and verify it does not trigger a second
   client query or expose the prior rows.

Record only revision names, synthetic fixture identifiers, counts, safe error
codes, and redacted execution IDs. This Stage 4B procedure remains pending
until it is actually run against disposable PostgreSQL with TLS.

## Adapter and HTTP integration checks

Start the backend against one of the disposable application databases with
connectors enabled, the explicit egress acknowledgement set for this test
only, and a generated test Fernet key. Exercise the real HTTP API with two JWT
users and the client fixture:

1. The owner tests the TLS connection, receives only the eligible allowed base
   table names, creates a source with an explicit selected table, lists it,
   reads its scope, and deletes it.
2. The other user receives the non-enumerating not-found response when reading
   or deleting the owner's source. Missing, API-key-only, and invalid
   authentication receive authentication failure before an adapter call.
3. A scope containing a now-ineligible table fails creation and leaves no
   source, credential, or scope row. Create the later table and show that it
   does not appear in the existing saved scope.
4. Confirm the ciphertext at rest does not contain synthetic username,
   password, or CA markers. Confirm API bodies and captured application logs
   omit those markers. Remove the active encryption key and confirm test/create
   fails safely while list/delete still work.
5. Repeat test connection attempts with wrong credentials, an untrusted CA,
   and a certificate whose SAN does not match the supplied hostname. Each must
   return its safe error envelope; no raw driver error or secret may appear.
6. Use a deterministic fixture fault (or a query cancellation proxy) for
   connection timeout, statement/operation timeout, cancellation, and inventory
   limit overflow. Confirm every opened connection closes and overflow fails
   rather than returning a truncated selection.
7. Start the backend with connectors disabled and verify ordinary file-upload
   application startup still succeeds. Do not use the client fixture in that
   check.

## Frontend integration and rendered checks

Run the frontend against that backend and a JWT-authenticated browser session:

1. Open **Database**, test the connection, select a subset of tables, save,
   list, inspect scope, and delete. Confirm the API prefix and response/error
   envelopes match the backend.
2. Change connection settings while a test request is outstanding. Its old
   response must not restore a stale table selection. Check double click and
   retry behaviour produces at most one saved source. Confirm a missing JWT is
   rejected before fetch and that an explicit 401 refresh is only replayed
   after the original create request was rejected.
3. Exercise existing file upload, switch from **File** to **Database** and
   back while upload/polling is active, and confirm it is not reset or aborted.
4. Inspect desktop and narrow layouts with keyboard navigation: inactive File
   and Database panels are not focusable; labels/errors, long table names,
   filtering, loading/empty/error/success states, selection, and deletion
   confirmation remain usable. The UI must state that a saved connection is
   not catalog-ready or database-chat-ready.

## Teardown and recorded evidence

Record only test names, status codes, migration revisions, row counts, and
redacted error codes. Never record secrets or the test CA private key. After
the run, validate the exact disposable project name and remove only its named
containers, networks, and volumes; do not use a broad Docker prune command.

The Stage 2D completion report must separately state whether this procedure
was actually run. A passing unit suite or frontend build is not a substitute
for the real PostgreSQL, TLS, HTTP, and rendered-browser checks above.
