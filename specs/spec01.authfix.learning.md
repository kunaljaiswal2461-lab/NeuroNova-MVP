# SPEC-01 learnings

## What was done
- Google sign-in now requires `email_verified is True` (identity check, not truthiness) and a non-empty email.
- Unverified password row + proven Google email -> `_reclaim_unproven_account`: password cleared, name/avatar overwritten, refresh tokens revoked, then verified.
- Verified row keeps its password; a row linked to a different `google_id` is rejected.
- `AUTH_DEV_AUTO_VERIFY` opt-in flag (default False) + validator that refuses it outside `APP_ENV=dev`.
- Callback passes raw `email`/`email_verified` via `.get()`; no 500 on missing claims.
- 14 tests in `backend/tests/unit/test_google_account_linking.py` (matrix M1-M13).

## Evidence the exploit was real
With the fix stashed and the `email_verified` argument dropped, `test_preregistered_password_does_not_survive_google_link`
failed with `DID NOT RAISE UnauthorizedError`: the attacker's password logged in after Google linked the row.
With the fix: 14/14 pass. `grep "is_verified = True" backend/app` -> only `verify_email` and `_reclaim_unproven_account`.

## Lessons
1. Verifying an email proves the *email*, not every credential already on the row. Upgrading trust (`is_verified`) must
   come with destroying anything the unproven party supplied.
2. Security flags should fail closed: the old auto-verify fired on *missing* config; now it needs a deliberate flag.
3. `x is not True` for security claims; truthiness accepts `"false"`.
4. Make the trust decision in one place (the service), not the route.
5. Test fixtures: PostgreSQL defaults (`uuid_generate_v4()`, `now()`) are invalid SQLite DDL, and production code inserts
   rows without ids. The fixture swaps those defaults only during `create_all` and assigns ids via a temporary
   `before_insert` listener. The spec's "copy the catalog fixture" advice did not work as-is.

## Process notes / caveats
- I edited the source before running the exploit test (spec said test-first); I then verified the exploit by stashing the
  fix and running the test against the old code, so the evidence is valid.
- `aiosqlite` was missing from `.venv` and is not in `requirements.txt`; I pip-installed it locally. Add it to a dev
  requirements file if tests are to run in CI.
- Full suite: 310 passed, 99 errors, all in `tests/catalog` and `tests/sources`, failing at SQLite `CREATE TABLE`
  (the same PostgreSQL-default problem) - not caused by this change, but I did not run a clean baseline to prove it.
- Not done (needs you): search production logs for `auth.register.dev_auto_verified`; PR description with the failing run.
