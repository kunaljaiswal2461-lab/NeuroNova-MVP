# Stage 9e — Sample values for text columns (SPEC-07, Part 4)

When the model writes `status = 'paid'`, it should already have seen that the data says `PAID`, `pending` and
`Cancelled`. This stage collects up to 20 example values per low-cardinality text column during the catalog sync,
stores them with that snapshot, and lets the schema context (Stage 9d) print them under the column:

```text
  segment text
    values: ["Retail", "SMB", "Enterprise", "enterprise "]
```

## Policy change (read this first)

[Stage 6A](stage6a-optional-database-profiling.md) says text values "are not persisted or returned". That is still
true **for profiling**. This feature is a separate path and **does store client values** in the application database
(table `catalog_column_samples`). Owner decisions of 2026-10-09:

| | Decision |
|---|---|
| D1 | Values are stored in the app DB, collected on each catalog sync and tied to that snapshot's columns. |
| D2 | Collection is **on by default**. The owner can switch it off per source; switching off **deletes** stored values. |
| D3 | Personal data stays out by **auto-detection plus owner per-column exclusions**. |

The values are sent to the model (as untrusted, fenced data). They are **never returned by an API**.

## How values are collected

Inside `CatalogDiscoveryWorker`, after discovery and before publish, when `sample_values_enabled` is true. The step
uses the discovery credentials and the Stage 4B restricted session (read-only transaction, `search_path=pg_catalog`,
`postgres_execution_*` timeouts, `client_connection_check_interval`, relation identity re-check under an ACCESS
SHARE lock, owner/RLS/superuser/partition/inheritance refusal, live column check). One connection per table.

1. **Candidates.** `text`, `varchar`, `bpchar` columns of the selected scope, minus owner exclusions and personal-data
   names.
2. **`pg_stats` (default).** One query per table reads `n_distinct`, `null_frac`, `most_common_vals`,
   `most_common_freqs`. No table scan. A column is kept when the estimated distinct count is ≤ 50, the MCV list is
   non-empty and `sum(freqs) + null_frac ≥ 0.95`.
3. **Exact fallback.** Only for a column with **no** `pg_stats` row, and only if the table is ≤ 64 MiB. A
   `GROUP BY ... LIMIT 51` runs behind the EXPLAIN cost gate and the per-row byte guard. A larger table without
   statistics is reported `no_statistics` and never scanned.
4. **Value rules.** NULL dropped; ordered by frequency, then code point; at most 20 values; kept exactly as stored
   (case and trailing spaces); each value cut at 64 code points (stored as 65 so Part 3 renders `…`).
5. **Personal data.** Any value matching a PII pattern drops the whole column (`pii_values`), judged before the cap.
6. **Failure.** Any error, timeout or permission denial records `error` for that table and the sync goes on. The whole
   step is bounded by `sample_values_total_timeout_seconds` (default 30). The sync lease keeps renewing meanwhile.
7. **Publish.** Samples are stored in the snapshot's transaction, only if the switch is still on and
   `sample_values_generation` equals the value captured before the first remote call; otherwise the snapshot is
   published without samples.

### Personal-data rules (heuristics)

* **Names.** The column name is split on non-alphanumerics and camelCase and skipped if any token is one of
  `email mail phone mobile tel telephone fax address street addr zip zipcode postcode postal pincode pin ssn aadhaar
  aadhar pan passport dob birth birthday password passwd pwd token secret salt hash ip ipaddr iban ifsc card cvv account
  acct username login`. A column `name`, or `first|last|full|given|family|middle` + `name`, or `surname`, is skipped
  when the **table** name has a token `customer user employee person people contact member patient student client staff`
  (plural tolerated).
* **Values.** After NFKC folding and mapping any Unicode decimal digit to ASCII: an e-mail shape, a UUID, an IPv4
  address, a value that is 8 or more digits after removing spaces, `-`, `(`, `)`, `+`, `.`; a run of 9 or more digits;
  a 13–19 digit run.
* Code: `app/services/sample_values/pii.py`. These are heuristics. **The owner's exclusion list is the backstop.**
  Known false positives: version strings such as `v1.2.3.4`, ISO dates stored as text.

## Skip reasons

`excluded_by_owner`, `pii_name`, `pii_values`, `high_cardinality`, `low_coverage`, `no_statistics`, `not_text`
(reserved: non-text columns are not candidates, so nothing records it today), `error`.
They are cached on the snapshot (`catalog_snapshots.sample_skips`, a JSON list of names and reasons, never a value).

## API (owner only; non-owners get the usual 404)

* `GET /api/v1/sources/{id}/sample-values` →
  `{enabled, sample_values_generation, excluded_columns: [{schema, table, column}], collected: [{schema, table, column, method, value_count}], skipped: [{schema, table, column, reason}]}`
  for the head snapshot. **Counts only, never a value.**
* `PUT /api/v1/sources/{id}/sample-values` with `{enabled?, excluded_columns?}`. `excluded_columns` **replaces** the
  list. Any change bumps `sample_values_generation`. `enabled=false` deletes every stored sample of the source in the
  same transaction. A new exclusion deletes that column's samples (in every snapshot) at once. The PUT never contacts the
  source; new values appear at the next sync.

## Schema context

`build_schema_context(...)` uses the stored values by default when the source has the switch on. Pass
`sample_values=None` to disable (Part 5's with/without comparison). Exclusions and the name rule also apply at read
time, so an exclusion added after collection hides the column immediately. The render format is Stage 9d's.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `sample_values_max_distinct` | 50 | keep a column only if its estimated distinct count is at most this |
| `sample_values_min_coverage` | 0.95 | `sum(freqs) + null_frac` needed (1e-6 tolerance for float4 stats) |
| `sample_values_exact_max_bytes` | 64 MiB | largest table the exact fallback may scan |
| `sample_values_total_timeout_seconds` | 30 | ceiling for the whole collection step of one sync |

## Limits to know

* `pg_stats` exists only after ANALYZE and is only as good as the sample. A table where nearly every value is distinct
  has no MCV list (`low_coverage`): this includes small name-like lookup tables (the fixture's `regions.name`).
* Planner statistics can miss rare values (the fixture's one hostile refund reason is absent for that reason). They
  can also miss values added since the last ANALYZE.
* `bpchar` (`char(n)`) values are cast with `::text` on the exact path, which drops the padding.
* The values are heuristically screened for personal data, not proven free of it.

## Operations

Migration `0018_catalog_column_samples` adds the two `data_sources` columns, `catalog_snapshots.sample_skips`, a
unique key `(source_id, snapshot_id, id)` on `catalog_columns` (needed as the target of the composite foreign key),
and the tables `catalog_column_samples` and `source_sample_exclusions`. Downgrade drops them all.
