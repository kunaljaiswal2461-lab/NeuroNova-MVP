# SPEC-02 — What I learned implementing it

## The bug in one paragraph
A JWT is only as trustworthy as its signing secret. `config.py` had `jwt_secret_key = "change-me-in-production"` as a *default*, so a server with no `JWT_SECRET_KEY` set started happily and signed every token with a string that is public on GitHub. Anyone can then mint a valid access token for any user whose UUID they know, or a password-reset token and take the account permanently.

## How serious is it?
**Critical if production runs on the default; unknown until you check.** The code can't tell us which. Three things make it worse than a normal misconfiguration:
- **Fail-open design:** nothing errored, nothing logged, `.env.example` never mentioned the variable. The unsafe state was the default state.
- **Undetectable:** a forged token is byte-identical to a real one (no `jti`, no request logging on `forgot_password`).
- **Not reversible by rotation:** a forged reset token leaves the attacker holding a real password. Rotating the key doesn't remove that, so the runbook (§11 step 7) forces a password reset for everyone if the default was live.

The main limiter is that the attacker needs a victim's UUID. That's weak protection, since UUIDs aren't secrets.

A second bug made it worse: the `Dockerfile` baked in `APP_ENV=dev`, so production could believe it was a laptop. That also quietly weakened SPEC-01's "auto-verify only in dev" guard.

## Design ideas worth remembering
- **Fail closed or fall back to something still secure.** Prod/staging: refuse to start. Dev: use a random per-process key (secure because nobody knows it; the only cost is that tokens die on restart).
- **An image describes what to run, not where it runs.** Removing `APP_ENV` from the Dockerfile (rather than changing it to `prod`) is what makes "required, no default" actually fire.
- **A denylist for one known-compromised value** is rejected in every env, so no old `.env` can keep it alive. It is not a strength check.
- **One accessor** (`jwt_signing_key`) means the secret is unwrapped in exactly one place; `grep` for `jwt_secret_key` outside `config.py` is an acceptance check.
- **Test-first exploit tests** (E1, E2) must fail on the old code. A test that never failed proves nothing.

## Things the spec didn't foresee (found while implementing)
1. **Pydantic leaked the secret in validation errors.** Test T9 failed on the first run: a `model_validator` error includes `input_value={...'jwt_secret_key': 'leakcheck-short'}`, the whole input dict. `SecretStr` doesn't help because the input is raw. Fix: `hide_input_in_errors=True` in `Settings.model_config`. Side effect: all `Settings` validation errors now omit input values, which is fine for config.
2. **More test files read `settings.jwt_secret_key`** than §7.5 lists (sources, conversational, test_source_*, etc.). They'd break with `SecretStr`, so I renamed them to `jwt_signing_key` too. The spec's "touch only these files" list was incomplete.
3. **Startup logging moved to `lifespan`**, right after `configure_logging()`. In `create_app()` (the spec's location) structlog isn't configured yet.
4. **Tests need a DB on `localhost:5433`** for some suites; without it they hang rather than fail, so I ran them file by file with a timeout.

## Python/pydantic mechanics I used
- `SecretStr | None = None` + an `after` model validator that may *assign* a field (`self.jwt_secret_key = SecretStr(...)`).
- `PrivateAttr` for a flag nobody should be able to configure, exposed via a read-only property.
- `get_settings()` is `lru_cache`d, so the ephemeral dev secret is stable within one process.
- `load_dotenv(override=False)` runs at import time, which is why `tests/conftest.py` sets `APP_ENV` at module top level, not in a fixture.
- `pgrep -f` / `pkill -f` match their own shell's command line. I killed my own shell twice doing that. Use a script file or `ps | grep "[p]attern"`.

## Not done by me (needs Siddhant, §11)
Steps 1–4 before merging: check whether prod uses the default secret (offline, with your own token), confirm `RENDER=true` exists on the service, set a generated `JWT_SECRET_KEY` and `APP_ENV=prod` on Render. **Without those, the next deploy will fail on purpose.** Step 7 (forced reset) only if step 1 shows the default was live.
