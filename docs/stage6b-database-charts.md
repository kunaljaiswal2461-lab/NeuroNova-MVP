# Stage 6B — Database-result and profiling charts

Stage 6B is a presentation-only extension of the controlled PostgreSQL result
and the persisted Stage 6A aggregate-profile views. It creates no connector,
job, query, profiling run, LLM request, migration, or chart persistence.

```text
already-authorized typed result or persisted profile observations
  -> deterministic local eligibility check
  -> table remains the exact record
  -> optional bounded chart with exact-value tooltip
```

## Result-chart boundary

The database-chat view offers a table/chart switch only for a successful,
already returned result with exactly two columns and at most 100 displayed
rows. The first column must be one unambiguous category (`text`, `boolean`, or
`uuid`) or one temporal value (`date`, `timestamp`, or `timestamptz`). The
second must be one finite, safely representable typed numeric value. No data is
fetched or recalculated when switching views.

The renderer never groups, deduplicates, averages, fills missing values, or
chooses a series. A repeated category or temporal instant, missing value,
unsupported shape, invalid time, excessive magnitude/precision, or a
precision collision falls back to the table with a concise explanation.
Categories retain returned ordering. Temporal rows are copied and ordered only
by their stated time semantics: date-only labels remain calendar dates,
timestamps without a zone retain their wall-clock fields, and `timestamptz`
values are ordered by their stated instants. Browser-local conversion is not
used for labels.

Exact numeric strings remain in the table and tooltip. Chart coordinates and
axis labels are explicitly approximations after a conservative numeric safety
check; the chart is unavailable rather than silently rounding an unsafe value.
Returned-result coverage distinguishes a requested Top-N result from safety
truncation and from a complete calculation. Existing execution, source,
snapshot, metric, and approved-relationship provenance remains beside either
presentation.

## Profile-chart boundary

The profiling view may render observed null-rate bars only from compatible,
persisted `COMPLETE` `OBSERVED_SAMPLE` table and column observations. Each
chart uses one table-observation identifier; observations from different
tables or samples are never combined. The chart retains collection time,
observed row count, sampling method, target percentage, limit, seed, warning
codes, and the explicit observed-sample limitation. It is not a full-table
claim and does not replace the aggregate observation list or deterministic
findings.

## Accessibility and failure behavior

The database result switch uses native buttons with tab roles and retains the
full accessible table view. Long labels are clipped only in chart axes; they
remain complete in tooltips and tables. A rendering failure returns to the
table, makes no network request, and does not rerun the query or profile.

## Verification boundary

Focused frontend tests cover eligible category and temporal results, duplicate
categories/times, missing/non-finite/precision-unsafe values, exact numeric
text, requested Top-N coverage, sample-qualified profile charts, and existing
database-chat/profile flows. Frontend lint, build, and repository whitespace
checks are run with the implementation. Real-browser responsive rendering and
tooltip interaction remain pending until inspected in a browser session.
