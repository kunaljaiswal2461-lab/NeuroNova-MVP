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
