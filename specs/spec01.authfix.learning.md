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
  (the same PostgreSQL-default problem). A baseline on the untouched original code gave 296 passed and the same 99
  errors, so they are not caused by this change.
- Not done (needs you): search production logs for `auth.register.dev_auto_verified`; PR description with the failing run.

---

# Addendum A (spec01.addon.pr.md): legacy accounts, signup dead-end, reproducible tests

## What was done
- **A1 (P1) legacy auto-verified rows:** new `users.password_proven_at` (migration `0016_password_provenance`, no backfill,
  revokes refresh tokens of all password users). Google linking now reclaims a row if it is unverified **or** has a password
  with no proof. `login_user` raises `PasswordResetRequiredError` (401, code `password_reset_required`) for an unproven
  password, checked after the password check so a wrong password leaks nothing.
- Provenance is set only by: dev opt-in registration, the `verify_email` unverified->verified transition, and `reset_password`.
- **A2 (P2) signup dead-end:** `email_service.delivery_configured` + `EmailDeliveryUnavailableError` (503). `register_user`
  (before creating the row) and `forgot_password` (before the user lookup) now fail loudly when no email can be sent; the dev
  message tells the developer to set `AUTH_DEV_AUTO_VERIFY=true`. `.env.example` is unchanged on purpose.
- **A3 (P2):** `aiosqlite==0.22.1` pinned in `backend/requirements.txt` (the version already installed locally).
- Verification email now says "Do not click the link" if you did not sign up.
- Tests: 11 new/changed (M14-M23, M12/M13 updated) -> 25 tests in the linking file; migration-graph test head updated to 0016.

## Evidence
- M14 (`test_legacy_verified_row_with_unproven_password_is_reclaimed`) failed on the SPEC-01 head:
  `assert '$2b$12$...' is None` on `hashed_password` - the squatter's password survived the Google link. Passes after the change.
- Full suite: **320 passed, 0 failed, 99 errors** (same pre-existing 99 SQLite-DDL errors in `tests/catalog` and `tests/sources`).

## Lessons
1. One boolean (`is_verified`) answered two questions (inbox proven? password proven?). When a flag's history is not recorded
   you cannot repair it later; record provenance as a timestamp and treat NULL as untrusted (fail closed).
2. A fix that only covers new rows leaves existing data exploitable. Ask "what is already in the database?" for every
   security fix. Searching logs is not a fix.
3. A request that depends on email delivery must not report success when nothing can be sent (fail-silent -> 201 then
   permanently unusable account). Check preconditions before creating state.
4. Never put a security bypass in a config template, even commented-in "for convenience": `APP_ENV` defaults to dev.
5. Put the new error check before the user lookup (`forgot_password`) so the response does not reveal whether an email exists.
6. A test that hardcodes the migration head (`test_migration_revision_graph`) must be updated with every new migration.
7. Fixture edge: inactive legacy rows are reclaimed in-memory before the `is_active` check, but the exception means nothing
   is committed, so the row is unchanged (M10 still passes).

## Caveats / not done
- `grep "password_proven_at = "` finds `verify_email` and `reset_password`; `register_user` sets it via a keyword argument
  (`password_proven_at=...`), so the spec's grep check shows only two hits.
- Not verified: a fresh virtualenv install from `requirements.txt` only (A3 acceptance), `alembic upgrade head` / `downgrade -1`
  against real PostgreSQL data, and the migration `UPDATE` (no PostgreSQL available here; review it in the PR).
- Deploy runbook (spec B7) is for you: confirm Resend works, `APP_ENV=prod`, rotate `JWT_SECRET_KEY`, run the audit query,
  tell existing password users they must reset once.
- Nothing committed; the spec files and these changes are in the working tree.
