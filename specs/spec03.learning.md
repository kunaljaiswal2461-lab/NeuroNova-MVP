# SPEC-03 (Part 0) — What I learned implementing it

## The point in one paragraph
An LLM writing SQL against customer data is only as good as the instrument that measures it. Part 0 builds the instrument first: a deterministic hostile Postgres fixture, a grader that compares *results* (not SQL text), a 150-question golden set with reviewed reference answers, a mutation self-test for the grader, and a runner that prints every accuracy with its 95% interval and blocks only on a real regression or a CRITICAL. No production code changed.

## It found a real production bug on its first run
`CatalogDiscoveryWorker` fails against **any** real PostgreSQL source whose selected tables have a primary or unique key: `selected_table_unavailable`.
- Cause: `app/connectors/postgres_adapter.py` reads `pg_constraint.contype` (type `"char"`). asyncpg returns that as **`bytes`** (`b'p'`). The code does `str(row["key_kind"]) not in {"p", "u"}`, and `str(b'p')` is `"b'p'"`.
- Why nobody saw it: the unit tests feed hand-written string rows (`"key_kind": "p"`). Only a real driver on a real database shows it. This is exactly what a fixture is for.
- Proof it is the only blocker: with a throwaway in-process decode of that one field (scratch script, not in the repo), `bootstrap()` succeeds end to end (source created, catalog snapshot published).
- **Not fixed here**, because INV-P0-10 says stop and report. The fix is one line: decode bytes before the `str()` (e.g. `key_kind = row["key_kind"]; key_kind = key_kind.decode() if isinstance(key_kind, bytes) else str(key_kind)`), plus a unit test that feeds `b"p"`. Test B1 is a **strict xfail** naming the bug; once fixed it XPASSes, which strict mode turns into a failure, so the marker cannot be forgotten.

## Things the spec got wrong or didn't foresee
1. **"Every boundary order lands in a different month when bucketed in UTC" is half true.** 00:15 IST on the 1st is 18:45 UTC the *previous* day (different month). 23:45 IST on the last day is 18:15 UTC the *same* day (same month). I planted both kinds; the late ones still catch `BETWEEN '…-01' AND '…-31'`, which drops the whole last day.
2. **Symmetric boundary rows cancel.** IST-March and UTC-March differ by a 5.5 h slice at *each* end. Equal numbers of planted rows at the start of March and the start of April made the two counts differ by 1 (then by 0 is one RNG draw away). Planting counts per month are now deliberately unequal (`EARLY_PER_MONTH`), giving a gap of 15, and GEN5 demands ≥ 5.
3. **`docker compose run` re-runs completed one-shot dependencies.** My `certs` service regenerated the CA and every password under databases that were already running, so the runner could not log in. Generation is now idempotent per volume (`down -v` gives a fresh set; a near-expiry cert fails loudly instead of rotating).
4. **Passwords cannot be set by the runner** (spec §8.4 puts that in `runner/entrypoint.sh`): the runner has no socket into the client DB container. The `certs` service writes the secrets and `client/05_passwords.sh` applies them through the local trust socket during init. Runner entrypoint just reads them.
5. **`page_views.path` has 200+ distinct values** because the SQL appends product ids. A "views per path" question exceeded the 200-row cap (LL4 caught it); the question now names the five fixed pages.
6. **Ties inside an ordered result, not only at the LIMIT cut-off, break row-by-row grading.** A correct candidate may order equal keys differently. I added lint rule **LL6** (extension to the spec's LL1–LL5). It also caught a cut-off tie (`time-010`) after the data was regenerated.
7. **Regenerating data invalidates facts baked into questions** (a duplicated name, which order has events, thresholds). The linter proves the SQL runs and has no ties; it does not prove a question still means what its wording says. Re-read affected questions after any generator change; a degenerate-result scan (empty / all-zero references) is cheap and worth keeping.
8. **Python 3.11 in prod, 3.12 locally.** The runner image uses 3.11 to match the Dockerfile.

## Design ideas worth remembering
- **Compare results, not SQL.** Then be lenient only where the answer is genuinely the same (names, column order, bounded extra columns, row order when unordered, `5` vs `5.00`, float noise) and exact everywhere else.
- **Test the grader before trusting it.** The mutants candidate is the grader's own test: 426/426 equivalent rewrites accepted, every wrong mutant either killed or explicitly accepted.
- **A survivor means one of two things**: grader too lenient, or the data cannot tell right from wrong. The runner classifies them (`grader_lenient` vs `data_insensitive`). Here: 0 lenient. 44 data-insensitive, all accepted with a reason in the question's `notes`: upper bounds beyond the data (it ends 14 June), aggregate-vs-aggregate comparisons that cannot tie, `INNER→LEFT` followed by another inner join, `COUNT(col)` on a column with no NULLs in the counted rows. Raw kill rate **90.4%**, adjusted **100%**; both are in the report.
- **Strengthen the data, don't just accept.** Going from 70% → 90% raw came from planting exact-boundary rows (midnight on every month start, exact-threshold prices/amounts, NULL phones and refund reasons) so `>=` vs `>` and `COUNT(*)` vs `COUNT(col)` actually change answers.
- **Mutants must be right to be useful.** Two bugs in my own generator showed up as "survivors": flipping the case of `'Asia/Kolkata'` (time zone names are case-insensitive: an *equivalent* rewrite), and flipping INNER→LEFT on a NOT NULL foreign key (cannot differ).
- **One RNG method.** Only `.random()` is used; pick/permutation/ranges are written on top of it, and test GEN7 AST-scans the generator. A test that scanned too strictly flagged `args.seed` (argparse), which I renamed rather than loosening the scan.
- **Tie-break by paise.** Rankings must be tie-free, so the generator nudges money by one paisa until per-customer, per-product and per-region sums are all distinct. Planted exact values are *frozen* so the nudges never move them.
- **Mask `.env`.** The runner bind-mounts `/dev/null` over `backend/.env` (which holds real keys); `Settings` is built from the process environment only.

## Deviations from the spec (all deliberate)
- Table checksum uses the spec's formula, `md5(string_agg(t::text, '|' ORDER BY pk))`, run with `TimeZone=UTC`. F9 (rebuild gives identical checksums) needs the Docker socket, so it is `fixture/verify_rebuild.sh` (host and CI), with F5 asserting against a manifest generated on a different build.
- Runner env/secrets come from the `certs` service (see 4). Added `16_derived.sql` (view and materialized view must follow the data load) and `client-postgres-alt.nn-eval.test` (a resolvable name the certificate does not cover, for F2).
- Learning file is `specs/spec03.learning.md` (your instruction) rather than the spec's `spec03.p0-eval-harness.learning.md`.
- `accuracy` uses *questions* (not runs) as `n` in the interval so `--repeat 3` does not make a number look three times more certain.
- Added LL6 (above) and the `accept-survivor:<kind>` convention for mutants.

## Dependency versions (pinned in `requirements.txt`)
`sqlglot==30.21.0`, `PyYAML==6.0.3`, `hypothesis==6.168.5` (test-only).

## Results
- `pytest -q` (backend): **451 passed**, 27 skipped (live/model), 1 failed + 99 errors that were already there before my change (the repo's SQLite fixtures use Postgres-only `uuid_generate_v4()` defaults, plus one more). Baseline before: 335 passed, same 1 failed + 99 errors.
- Live layer: 26 passed + the B1 strict xfail. `evals.lint --live`: 160 questions, 0 errors. `reference`: **100%** on golden + adversarial. `mutants`: equivalence **100%**, kill rate raw 90.4% / adjusted 100% (44 accepted).
- Golden set: exact distribution of §11.3 (150 questions + 10 adversarial seeds), every one of the 16 output types ≥ 3 times, 7 `time_bucket` questions tagged `tz_boundary`, 4 fan-out join questions.

## Python/tooling mechanics I used
- sqlglot AST rewrites (`exp.Select`, `args["order"]`, `args["from_"]` in this version) for mutants and for the LL3/LL6 sort-key probes; positional `ORDER BY 2` / `GROUP BY 1` must block column reorders and alias renames.
- `pytest-asyncio` 0.25: a session-scoped async fixture needs `loop_scope="session"` on the fixture *and* the tests using it.
- asyncpg: `"char"` is `bytes`; `prepare().get_attributes()` gives column names and pg type names; a read-only `cursor` caps rows.
- `docker compose run` starts (and re-runs) `service_completed_successfully` dependencies; `pg_isready` over the socket is true during the init-phase temp server, so health-check over TCP.
- A pipe into `head` closing early killed a `docker compose run`; write to a file instead.

## Not done by me — needs Siddhant
1. **Review every golden question (§11.6)** and set `reviewed_by: siddhant`; until then `python -m evals.lint --live --require-reviewed` fails by design (INV-P0-09). Look especially at: the looser `output_type` labels I had to assign to reach 3 per type (`pivot`: join3-003, join3-007; `takeaways`: grp-010, join3-011, win-002; `narrative`: sub-004; `heatmap`: win-004, time-012), the "`date` month labels" convention in time questions, and the 44 accepted-survivor notes.
2. **Approve the production fix** for the `"char"` bytes bug (above) and remove the B1 xfail.
3. **Enable GitHub Actions** and add secrets `OPENAI_API_KEY` and `STAGE4C_EVALUATION_ACK` (environment `model-eval`). The workflow has not run anywhere yet.
4. **Optional baseline**: `stage4c --repeat 3` once, commit `baselines/stage4c.json`. The candidate's decoder is unit-tested; its end-to-end path needs a key and was never executed.
5. `pricing.yaml` is empty on purpose (costs are omitted rather than guessed).
6. The private-key / `sk-` `git grep` and the empty `git diff main -- backend/app backend/agentic_engine` were checked locally (both clean); neither is automated in CI.
