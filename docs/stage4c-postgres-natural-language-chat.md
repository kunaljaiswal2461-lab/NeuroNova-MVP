# Stage 4C — PostgreSQL Natural-Language Chat

Stage 4C adds a source-bound, JWT-only conversation surface for the existing
Stage 4A plan language and Stage 4B controlled executor. It does **not** add
raw SQL, joins, multi-source questions, profiling/findings, business semantic
definitions, charts, query repair, or a second database executor.

```text
JWT user + selected source + question
  -> source-owned conversation binding
  -> current published catalog head + unchanged selected scope
  -> deterministic single-table selection or clarification
  -> LLM structured PostgresQueryPlan only
  -> Stage 4B validation / execution / reauthorization
  -> typed bounded result + grounded chat explanation over SSE
```

## Binding and authorization

`conversation_sessions` now binds to exactly one resource:

- existing uploaded `dataset_id`, or
- new `source_id` plus a non-null user who is also the source owner.

Migration `0011_postgres_chat_bindings` preserves all existing dataset
sessions. It adds a database XOR check and a `(source_id, user_id)` composite
foreign key to the source owner, in addition to source deletion cascade. A
source deletion cascades its sessions and messages, so a historical database
result cannot be read after the source is deleted. Generic session read,
transcript, delete, and send routes resolve this binding centrally; legacy
API-key access is rejected for source sessions.

Source session endpoints are JWT-only:

- `POST /api/v1/sources/{source_id}/chat/sessions`
- `GET /api/v1/sources/{source_id}/chat/sessions`

The shared session/transcript/SSE endpoints remain the persistence and stream
surface. A session or source ID is never an authorization capability.

## Planner boundary

Before an LLM is called, the server verifies that the source has exactly one
current published catalog head from a successful run, with an unchanged,
non-empty selected scope. Historical snapshots can still be inspected but
cannot plan a calculation.

Table choice is deterministic and bounded: schema/table names are scored
first; a small candidate set can be expanded by column names; a tie or no
decisive table produces a clarification. The system never chooses the first
table. Only the selected table's bounded structural columns reach the model.
Database comments and chat text are marked untrusted data, and credentials,
connection details, raw rows, unrelated tables, and historical result rows
are excluded from prompts.

The model uses JSON mode and may return only one strict envelope:

```json
{"kind":"plan","plan":{"schema_version":1,"asset_id":"…", "projections":[], "filters":[], "aggregations":[], "group_by":[], "sort":null, "limit":1}}
```

or a bounded `clarification` / `unsupported` envelope. The nested plan is the
frozen Stage 4A `PostgresQueryPlan`; it rejects SQL, unknown keys, free-form
identifiers, and invalid typed values. The server validates that every model
ID belongs to the selected asset and pins the catalog snapshot before passing
the plan to:

```python
await execute_postgres_query(
    get_sessionmaker(),
    owner_user_id=trusted_jwt_user_id,
    source_id=trusted_source_id,
    snapshot_id=pinned_current_snapshot_id,
    plan=trusted_structured_plan,
    settings=settings,
    cancellation=cancellation,
)
```

There is no automatic repair or retry. Relative dates require an explicit ISO
date/timestamp and, where applicable, an IANA time zone; the service does not
infer a browser or business timezone. Schema/catalog questions are answered
from authorized catalog evidence only. Questions about joins, findings,
profiling, charts, or cross-dataset claims are explicitly unavailable.

## SSE, cancellation, and retention

Source chat emits explicit `planning`, `executing`, `clarification`,
`unsupported`, `catalog`, and `postgres_result` events alongside the existing
`start`, `token`, `done`, and safe `error` events. `postgres_result` is the
Stage 4B typed result shape: its execution/source/snapshot IDs, columns,
typed values, ordering, timestamps, warnings, row/byte truncation, and
completeness semantics are retained unchanged. Integer and decimal values
remain strings and are not coerced by the UI.

The chat transcript retains one bounded `postgres_result` copy with the
assistant message so a correctly authorized user can reopen the result. The
retention rule is intentionally simple: it remains only until the user deletes
that session or the bound source is deleted (there is no separate Stage 4C
time-to-live job). Its maximum rows/bytes are the existing Stage 4B execution safety budgets
(`POSTGRES_EXECUTION_MAX_RESULT_ROWS` and
`POSTGRES_EXECUTION_MAX_RESULT_BYTES`); `complete=false` must be shown as a
partial result. `postgres_query_runs` remains redacted and stores no rows,
SQL, parameters, credentials, prompts, or driver messages. Retained result
rows are deliberately omitted from later LLM planning history.

The stream owns a `PostgresExecutionCancellation` handle. Browser cancellation
calls `POST /api/v1/chat/sessions/{session_id}/turns/{turn_id}/cancel` with the
same JWT owner before closing the SSE stream; a detected disconnect also
terminates the dedicated client connection. Cancellation that wins before the
final result write retains and emits no rows. A deleted source/session cannot
be read again, but its existing owner-bound live handle can still terminate an
already-authorized remote connection. No reconnect replays a message/query.
Cancellation coordination is process-local, like Stage 4B's execution limiter,
and is not a cross-replica guarantee.

## UI and operational limits

The database-chat route is `/sources/:sourceId/chat`, intentionally outside
the dataset-only route guard and `DatasetContext`. Catalog inspection exposes
the entry point only when a current published snapshot exists. The UI shows
the explicit binding, planning/executing state, clarification/unsupported
states, typed table, partial-result warning, safe failures, and a cancel
control. It does not show generated SQL/code or database charts.

Stage 2D live/disposable PostgreSQL verification remains pending. Unit tests
exercise contracts and safety boundaries but do not prove live TLS, ACL/RLS,
read-only enforcement, schema races, or cancellation behavior against a real
customer database. This change performs no deployment or production database
activation.
