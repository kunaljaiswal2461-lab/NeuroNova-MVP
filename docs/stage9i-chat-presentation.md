# Stage 9i — Chat presentation and production transport (SPEC-10)

## Stream limits (browser)

| Limit | Default | Error `code` |
|---|---|---|
| time to response headers | 75 s | `connect_timeout` |
| time without any bytes (frames or `: keepalive`) | 45 s | `stream_idle_timeout` |
| whole stream | 300 s | `stream_max_timeout` |

The server writes `: keepalive` after 10 s of silence and fails a turn at 150 s with `turn_timeout`. A stream is never
retried and never replayed after a 401; `ensureFreshToken()` refreshes an access token with less than 60 s left before
the send. After any turn that ends without `done`, the page reads the server transcript once (read-only) and shows that.

## Failure notices (D5)

A failure with no frame received is *pre-acceptance*: the bubbles are removed, the question goes back into the composer
(unless the user typed something new) and a notice explains: 401 sign-in expired; 409 earlier question still running;
429 "try again in about N minutes"; connect timeout or network error "the server didn't respond". An in-stream drop keeps
whatever arrived and shows a note. Nothing is ever re-sent automatically; Try again is a button.

## Formatting (D4)

Display only; the exact value is always on hover/focus and in the CSV. Locale `en-IN` (`12,34,567.50`). All number work is
string-based. In answer prose and KPI tiles a fraction longer than 4 digits (after stripping zeros) is rounded half-even to
2 digits and shown as `≈ 3.33`; tables never round. Dates follow a per-column granularity (year / month / day / minutes),
chosen from all values and only when there are at least two distinct ones. `timestamptz` is shown in `display_time_zone`
(IST by default); `date` and naive `timestamp` are never shifted. Axis ticks are compact (`1.2Cr`).

## Turn states (D7)

`pending → answer | clarification | unsupported | failed | cancelled | interrupted`. Each has its own card. The `token`
text of a SQL answer is not shown when a `presentation` exists (the prose comes from the segments).

## Production runbook

1. Backend first, then the frontend: the new `presenting` event and keep-alive comments are ignored by the old page.
2. Render: `APP_ENV=prod`, `OPENAI_API_KEY` set, `NL_SQL_STUB_LLM_PATH` empty, `NL_SQL_ROUTE_MODE=hybrid`, **one worker, one
   instance** (turn locking and the 409 are process-local), `FRONTEND_ALLOWED_ORIGINS` containing the exact Vercel origin.
   Vercel: `VITE_API_BASE_URL` is the absolute Render URL ending in `/api/v1` (Production and Preview).
3. Stream transparency: `curl -sN` the message route with timestamps; events must arrive spread over time and
   `: keepalive` about every 10 s; the largest gap must be 12 s or less.
4. Cold start, a 20-question latency sample, browser QA at desktop and 360 px, and the `typed_only` kill-switch drill, on
   a disposable source.
