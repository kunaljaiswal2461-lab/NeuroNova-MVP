# Stage 9h — Pass 2: answers, privacy and the number check (SPEC-09, Parts 8 and 9)

After the rows exist, a second model call chooses **how to show them** and writes the answer text. The model never
writes a figure that reaches the screen: it writes **references**, and the server replaces each with the exact string
of the value. A failure in pass 2 is never a failed turn: rows and SQL are still delivered, with the templated summary
and the table.

Code: `backend/app/services/nl_sql/` — `pass2_input.py`, `lineage.py`, `pass2_prompt.py`, `pass2_spec.py`,
`pass2_validate.py`, `pass2.py`, `display.py`; `app/services/column_privacy.py`; prompt `prompts/pass2_system.md` (`pass2-v1`).

## Consent (per source) and what the model may see

`data_sources.result_rows_to_model_enabled` (default **off**) with a `…_generation` that advances on every change.
`GET/PUT /sources/{id}/ai-settings` (owner only; another user gets 404).

| | Consent off | Consent on |
|---|---|---|
| Column facts (label, type, personal flag) | yes | yes |
| Row count, completeness | yes | yes |
| Statistics | numeric, non-personal columns: count, nulls, distinct, min, max, sum, mean | all columns: count, nulls, distinct; numeric non-personal also min/max/sum/mean; temporal non-personal also min/max |
| Row values | **none** | up to `nl_sql_pass2_max_rows` (50), every cell a fenced literal of ≤ 200 characters |
| Personal columns | no stats at all | placeholders `<label>_<n>` (per column, in order of first appearance); real values never leave |

Invariant I4 reads: **no row values beyond what the allowed numeric statistics already reveal.** A one-row
non-personal aggregate (`SELECT count(*)`) is its own min, max, sum and mean, so that single number is visible with consent
off; a one-row personal column is never visible (no statistics, and a placeholder with consent on). This matches the
settings text ("the model sees only column names and totals").

The placeholder map exists in memory for the turn only. It is not in `repr`, logs, SSE or the stored record.

## Personal data flags

`source_column_privacy(source_id, schema, table, column, state)`; `GET/PUT /sources/{id}/column-privacy`. The effective
flag is the owner's state if present, else the **suggestion**: Part 4's name rules (`email`, `phone`, a person's `name`
...) plus a text column whose name contains `comment`, `note`, `description`, `reason`, `message` or `address`. The
suggestion is computed, never stored, and **drives pass-2 masking only**. Stored sample values are hidden only by Part 4's
personal-data rules, an owner exclusion, or an owner's explicit "personal" mark, applied at read time (no re-sync).

An output column is personal only if **every** source column it reads is personal (found by parsing the validated
SQL). A column the parser cannot follow (star, subquery, CTE, `UNION`, a derived table) is treated as personal.
`count(...)` and constants read nothing worth protecting and are not personal.

## References and the number check

References allowed in `answer` and `takeaways`: `{{row:<i>.<label>}}`, `{{stat:<label>.<fn>}}` (`count nulls distinct
min max sum mean`), `{{rowcount}}`, and for `kpi_comparison` only `{{delta:<cur>,<prev>}}` / `{{delta_pct:<cur>,<prev>}}`
(computed with `Decimal`; `n/a` when the previous value is 0). A label containing `.` or `}` cannot be referenced. A row
reference needs consent; a reference to a personal column resolves to the **real** value (the owner is the viewer).

Every other number written in `answer`, `takeaways` or `caveats` must equal, as a canonical decimal, a value the model
was given: a result cell of a non-personal column (consent on), a statistic, the row count, a number in the question
or in the pass-1 assumptions, a year 1900–2100, or a whole number 1–10. A rounded figure is **not** a match; a
formatting difference (thousands separators, trailing zeros, a leading `+`) is. Digits are folded first (NFKC and other
scripts' digits), and English number words ("twelve", "two hundred and five") are read as numbers. A failing sentence is
dropped and the caveat *"Some text was removed because it contained a number that is not in the result."* is added;
an answer with no sentence left falls back.

## Order of checks (first hard failure → fallback)

1. Envelope (strict Pydantic, limits) → `spec_invalid`. 2. Canary or a 40-character verbatim run of the instructions →
`leak`. 3. Sanitise (no HTML, markdown links/images, code fences, control characters). 4. References → `bad_reference`
(also: a placeholder the model saw appearing in any shown text). 5. Number check → sentence dropped, or
`number_check_failed`. 6. Display fit: the model's choice, then the pass-1 intent, then `table`
(`display_downgraded:<from>-><to>` plus a caveat). 7. Mandatory caveats, added in code: partial result, hidden columns,
consent off, repaired query. 8. Resolution. Other fallbacks: `llm_unavailable`, `input_too_large`.

## Token budget

The whole pass-2 user message must stay within `nl_sql_pass2_budget_tokens` (6,000, o200k). Over budget, rows are cut to
20, then 5, then dropped (statistics stay); the cut is recorded in `pass2.budget_cut`.

## Delivery guard

Immediately before pass 2 the delivery guard is *entered and exited* (a check): if access was revoked after the query
ran, no row goes to the model and `access_revoked` is stored. The guard is **not** held during the model call; it is
entered again for persistence and again for the row-bearing frames.
