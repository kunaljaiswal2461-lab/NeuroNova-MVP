# Stage 9g — Hybrid database chat (SPEC-09, Parts 6, 7, 10, 11)

A database question is answered either by the **typed path** (a confirmed metric, unchanged) or by **model-written
SQL** that passes the validator and the restricted gateway first. The SQL path adds at most **one repair**, an
**answer-writing second model call** (Stage 9h) and a frontend that renders the result.

Code: `backend/app/services/nl_sql/` (`pipeline.py`, `repair.py`, `adapter.py`, `history.py`, `messages.py`,
`turn_record.py`), `backend/agentic_engine/conversational/postgres_chat_agent.py` (`_stream_sql_path`),
`neuronova-frontend/src/components/answers/`.

## Routing (setting `nl_sql_route_mode`, default `hybrid`)

| Step | Condition | Route |
|---|---|---|
| R1 | `typed_only` | the Stage 4C turn, unchanged (kill switch). No write guard is applied here either: the old path decides. |
| R2 | the question is a write/DDL request | refuse with the fixed text. **No model call on either path.** |
| R3 | a confirmed metric is named (`matched` or `ambiguous`) | the typed path |
| R3a | the typed planner answers `unsupported` | fall through to the SQL path **once** (`route = sql_after_metric_fallthrough`) |
| R4 | no metric, or metric lookup `unavailable` | the SQL path |

With no API key (and no stub script) the SQL path ends in `error {code: model_unavailable}`. The typed path is unaffected.
One database turn per session: a second `POST` while one is in flight gets HTTP 409 `{"error":{"code":"turn_in_progress"}}`
and writes nothing (process-local registry; a registration older than 15 minutes counts as abandoned).

## The repair state machine

`answer_question(...)` runs pass 1, `execute_validated_sql`, and at most **one** extra pass-1 call (a repair *or* a
regenerate), counted in code. After the extra call there is never a third.

| Outcome of an attempt | Class | What happens |
|---|---|---|
| success | — | answer |
| clarification / unsupported | — | final, no repair |
| validator rejection in C5's list, `query_error` with a known SQLSTATE, `query_too_expensive`, `unsupported_output_type`, `result_row_too_large`, `envelope_invalid` | repairable | one repair call with a **repair brief** |
| `schema_changed` | regenerate | pass 1 runs again against the new head, **no brief** |
| everything else (timeouts, access, cancellation, `llm_unavailable`, leaks ...) | final | no extra call |

The repair brief is built only from our own text: the previous SQL (as a JSON literal), `problem: <code> — <fixed
sentence>`, the validator's own message, the identifier and up to five catalog suggestions. It never carries Postgres
server text, DETAIL, CONTEXT, HINT, a row value or a SQLSTATE's message (`SqlExecutionResult.error_class` is the SQLSTATE
only). A repair that returns the same SQL (after whitespace and identifier-case folding) is **not executed again**.

## SSE contract (C1)

New event types: `sql_generated`, `repairing`, `presentation`.

| Path | Events, in order |
|---|---|
| typed path | as before: `start, planning, [executing], postgres_result \| clarification \| unsupported \| catalog \| error, token, done` |
| write guard | `start, planning, unsupported, token, done` |
| SQL → clarification / unsupported | `start, planning, clarification \| unsupported, token, done` |
| SQL → answer | `start, planning, sql_generated, executing, postgres_result, presentation, token, done` |
| SQL → repair → answer | `…, sql_generated, executing, repairing, sql_generated, executing, postgres_result, presentation, token, done` |
| SQL → repair after a rejection | `…, sql_generated, repairing, sql_generated, executing, …` (no `executing` before the repair: the event is emitted only after the validator accepts) |
| SQL → failed | `…, error, token, done` with `error.data = {code, message}` |
| cancelled | the existing cancellation path |

Payloads: `sql_generated {sql, assumptions, output_intent, rewrites, attempt}`, `repairing {reason_code}`,
`presentation {answer, takeaways, caveats, display, encodings, table_columns, fallback, derived}`.
No event carries row values outside `postgres_result`, placeholders, prompts, canaries, server error text or a stack trace.

## Persistence (C2)

`chat_messages.nl_sql` (JSONB, migration `0019_nl_sql_turns`; Pydantic `NlSqlTurn`, `extra="forbid"`) holds the route,
prompt versions, the **SQL**, assumptions, output intent, rewrites, intent warnings, the repair record, the outcome, the
failure code, the resolved presentation (with its templates) and the model-call totals. `postgres_result` keeps its
"never a query string" contract. `postgres_query_runs` still stores no SQL. Token usage on the message is the sum of
every model call of the turn.

## The four contract reversals (relative to the Build Plan)

1. **Model SQL is allowed** — after the validator (Stage 9b) and the restricted gateway (Stage 9c).
2. **One repair is allowed** — a second model call per turn after a failure (and one more call for pass 2).
3. **Result rows may go to the model** — only with per-source consent and column masking (Stage 9h).
4. **SQL is stored in the chat message only** — `chat_messages.nl_sql.sql` (and `repair.original_sql`). The Build Plan's
   row 4 is narrower here: `postgres_query_runs` still stores none, by owner decision.

## Follow-up questions

The last three turns are passed to pass 1 as context (`history_from_messages`): question, SQL, assumptions and output
type, every string a JSON literal. A typed-path turn contributes its question and `used confirmed metric <name>`; a
clarification contributes `asked: <text>`; a failed turn only its question. **Rows are never included.** Pass-1 prompt
`pass1-v2` adds one rule: carry filters forward only when the new question refers to them.

## Frontend

`AnswerView` (answer text, takeaways, amber caveats, the chosen display, the table behind a tab, assumptions, a SQL
panel with Copy, CSV export, footer) renders any assistant message that has both a `postgres_result` and an `nl_sql`
record. Typed-path messages keep the old `PostgresResultTable`. Sixteen displays, one renderer file each, all rendering
exact strings; the browser re-checks the backend's eligibility rule with the same fixture file
(`backend/tests/fixtures/display_eligibility.json`). Nothing in `components/answers` uses `dangerouslySetInnerHTML`
(a test greps for it). The CSV prefixes any cell starting with `= + - @ TAB CR` with `'`, so a negative number is text
in a spreadsheet by design.

## Fixed user-facing messages (C6)

All in `app/services/nl_sql/messages.py`. No model text and no server text is shown as an error. The only model text
shown is a *clarification question* (at most 300 characters). Message slots hold catalog names only: a column named in
the message is echoed only when it is a real catalog column.

## QA (manual, against the fixture with a real or scripted model)

25 scenarios for the tester; see `backend/evals/README.md` §T for the commands.

1. Single-number question (KPI). 2. Ranked list. 3. Bar. 4. Grouped bar. 5. Line. 6. Multi-line. 7. Area. 8. Pie with
≤ 6 slices. 9. Pie with 7 categories (must be shown as another display with a caveat). 10. Scatter. 11. Histogram.
12. Heatmap. 13. Pivot. 14. Narrative. 15. Takeaways. 16. KPI comparison with a delta. 17. A repair is visible ("First
query failed · correcting once…", the "Corrected automatically" chip). 18. SQL panel is collapsed; Copy shows "Copied".
19. CSV export opens in a spreadsheet, `=`-leading text is plain text. 20. Toggle "Send result rows to the AI model" and
a column's personal flag and watch the caveats change. 21. A second browser tab gets the 409 message while a turn runs.
22. Close the tab mid-answer, reopen: one cancellation message, no duplicate. 23. A Hindi question. 24. The planted
comment question (`adv-002`). 25. 360 px wide and dark mode.
