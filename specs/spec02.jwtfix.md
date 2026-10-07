# SPEC-02 — Forgeable tokens from a default JWT secret, and fail-open environment config

| Field | Value |
|---|---|
| Status | Proposed |
| Severity | **Critical** (conditional on deployment config, see §4) |
| Area | Configuration and token signing: `backend/app/core/config.py`, `backend/app/core/security.py`, `backend/app/core/dependencies.py`, `Dockerfile`, `.env.example`, `docker-compose.yml` |
| Audience | Siddhant (owner, reviewer) and Claude Code (implementer) |
| Code baseline | `NeuroNova-MVP-main` snapshot with SPEC-01 and Addendum A merged (migration head `0016_password_provenance`). Line numbers refer to that snapshot; re-verify before editing. |
| Depends on | SPEC-01 / Addendum A: their dev-only guards assume `APP_ENV` tells the truth. This spec makes that assumption hold. |
| Related, out of scope | Legacy `API_KEY` shipped in the frontend bundle (next spec), OAuth `state` CSRF, token storage in `localStorage` |

---

## 0. How to use this spec

**For Siddhant:** Part A explains the problem from first principles: what a signed token is, what each line of config does, and what an attacker can do when the configuration is wrong. It also covers a second problem found while writing this spec (the Docker image hard-codes `APP_ENV=dev`), which turns out to make the first one worse and quietly weakens SPEC-01. Part B is the fix and the reasoning for each decision. §11 is a deploy runbook you need to do by hand.

**For Claude Code:**

1. Read the whole spec before editing.
2. Work test-first in the order in §10. The two exploit tests in §9.1 must **fail on the current code** before anything is changed. If either passes on unfixed code, stop and report.
3. Touch only the files listed in §10.
4. **Never test against a deployed environment.** Every test runs locally with constructed `Settings`.
5. Validation error messages must **never contain the secret value**. There is a test for this (§9, T9).

---

# PART A — The problem

## 1. What the JWT secret protects

### 1.1 How a signed token works

When a user logs in, the backend hands them an access token. A JWT has three dot-separated parts:

```
base64(header) . base64(payload) . base64(signature)
```

- **Header:** `{"alg": "HS256", "typ": "JWT"}`
- **Payload:** `{"sub": "<user uuid>", "email": "...", "role": "USER", "type": "access", "exp": 1760000000}`
- **Signature:** `HMAC-SHA256(secret, header + "." + payload)`

The payload is **not encrypted.** Anyone can base64-decode it and read it. The only thing that makes the token trustworthy is the signature, and the only thing that makes the signature trustworthy is that **nobody except the server knows the secret.**

Here's a worked example of what the server checks:

1. The client sends `Authorization: Bearer <token>`.
2. The server recomputes `HMAC-SHA256(secret, header.payload)`.
3. If that equals the signature in the token, the server believes **every claim in the payload**, including `sub`, which says who you are.

Whoever knows the secret can write any payload and compute a valid signature. The server can't tell a token it issued from one the attacker minted. **The secret is the entire root of trust for user identity.**

### 1.2 Everything signed with this one secret

| Token | Created at | Verified at | What it proves when valid |
|---|---|---|---|
| Access token | `security.create_access_token` (L24–32) | `dependencies._resolve_user_from_jwt` (L66) | "I am user `sub`", on every authenticated request |
| Email-verification token | `auth_service.register_user` (L82) | `auth_service.verify_email` (L158) | "I can read this user's inbox", which flips `is_verified` and (since Addendum A) sets `password_proven_at` |
| Password-reset token | `auth_service.forgot_password` (L182) | `auth_service.reset_password` (L194) | "I can read this user's inbox", which lets you set a new password |
| OAuth `state` | `auth_routes.google_oauth_start` (L261–266) | `auth_routes.google_oauth_callback` (L302) | "This Google login was started by our server" |

Refresh tokens are **not** in this list. `create_refresh_token_pair` (security.py L35–39) generates random bytes and stores only their SHA-256 hash in the database. They don't depend on the JWT secret. That matters for the runbook (§11).

## 2. The vulnerable code, line by line

### 2.1 The secret's definition (`config.py` L105)

```python
105  jwt_secret_key: str = Field(default="change-me-in-production", min_length=16)
```

| Part | What it does | What's wrong |
|---|---|---|
| `default="change-me-in-production"` | If no `JWT_SECRET_KEY` environment variable exists, use this string. | The fallback is a **publicly known value**. It's in the source code, which is on GitHub. A missing environment variable doesn't cause an error; it silently produces a server whose secret everyone knows. |
| `min_length=16` | Rejects secrets shorter than 16 characters. | The default is 23 characters, so it **passes its own check**. The length check can never catch the one value that matters most. |
| `str` | A plain string. | Pydantic's validation errors include the input value. A plain `str` can end up printed in a startup traceback, and it shows up in `repr(settings)`. The connector keys in the same file already use `SecretStr` (L137, L141); this one doesn't. |
| No environment check | The same rule applies in `dev` and `prod`. | There's no validator saying "in production, this must be set and strong". Compare SPEC-01's `_auth_dev_auto_verify_only_in_dev` (L279–283): that pattern exists in this file, just not for the secret. |

### 2.2 The environment flag (`config.py` L78)

```python
78  app_env: Literal["dev", "staging", "prod"] = "dev"
```

| Part | What's wrong |
|---|---|
| `= "dev"` | **Missing configuration falls back to the least safe mode.** If `APP_ENV` is never set, the server believes it's a developer's laptop. Every safety rule written as "only allowed in dev" quietly allows itself. |

Code that currently trusts this flag:

| Where | Guard | What happens if `app_env` is wrongly `"dev"` in production |
|---|---|---|
| `config.py` L279–283 (SPEC-01 Rule 4) | `AUTH_DEV_AUTO_VERIFY` is refused unless `app_env == "dev"` | The guard passes. If the flag is also set, **every signup is auto-verified in production**, which is SPEC-01 Attack C again. |
| `auth_service.register_user` L60 | Auto-verify requires `app_env == "dev"` | Same as above. |
| `auth_service._require_email_delivery` L38 | The detailed config hint is shown only in dev | Production error messages advertise internal configuration. Minor. |

**SPEC-01's protection against Attack C is only as strong as the truthfulness of `APP_ENV`.**

### 2.3 The image hard-codes `APP_ENV=dev` (`Dockerfile` L41–45)

```dockerfile
41  ENV PYTHONUNBUFFERED=1 \
42      PYTHONDONTWRITEBYTECODE=1 \
43      PIP_NO_CACHE_DIR=1 \
44      APP_ENV=dev \
45      APP_HOST=0.0.0.0 \
```

Line 44 bakes `APP_ENV=dev` into **every container built from this image, including production**. A deploy platform's environment variables override it, but only if someone sets them. If nobody does, the production container is told it's in development.

Even making `app_env` a required setting wouldn't catch this: the variable *is* set, to the wrong value, by the image itself. An image describes *what to run*, not *where it's running*. The environment name belongs to the deployment, not the build.

### 2.4 Templates that seed every environment (`.env.example`, `docker-compose.yml`)

| File | Line | Content | Problem |
|---|---|---|---|
| `.env.example` | top | `APP_ENV=dev` | Fine for a local template, but nothing marks it as local-only. |
| `.env.example` | — | **No `JWT_SECRET_KEY` line at all** | A deployer configuring from this file is never prompted to set the secret, so the default from §2.1 is what runs. |
| `docker-compose.yml` | L11 | `APP_ENV: ${APP_ENV:-dev}` | Local compose; acceptable. |

### 2.5 Verification side, for completeness (`dependencies.py` L65–82)

```python
66  payload = decode_access_token(token, settings.jwt_secret_key)   # signature check
69  if payload.get("type") != "access":                              # token-type check
...
79  user = await db.get(User, user_id)                                # load from DB
80  if not user or not user.is_active:
```

This part is **correct**, and it limits the damage:

- **L69 type check:** a password-reset or verification token can't be used as an access token. Their payloads have `purpose` but no `"type": "access"`. The reverse holds too: `decode_one_time_token` requires a matching `purpose`.
- **L79 database load:** the role and active status come from the database, not the token, so forging `"role": "ADMIN"` gains nothing, and a disabled user stays disabled.
- **But L66 is the only check on identity.** If the signature verifies, `sub` is believed.

## 3. What an attacker can do

### 3.1 Preconditions

| Precondition | How likely |
|---|---|
| Production runs with the default secret | Likely unless someone deliberately set `JWT_SECRET_KEY`: the template doesn't mention it, and nothing fails when it's missing (§2.1, §2.4). **Check your Render service now (§11 step 1).** |
| The attacker knows the default | Certain. It's in the source. |
| The attacker knows the target's user UUID (for attacks on a specific user) | This is the real limiting factor. UUIDv4 values can't be guessed. `/register` doesn't return one, and `/login` returns only your own. I found no endpoint exposing other users' IDs. But IDs are **not designed as secrets**: they show up in logs, error reports, URLs, support screenshots, and any future feature that lists people. A defense that depends on IDs never leaking is fragile. |

### 3.2 Attack 1: become any user (needs their UUID)

```python
from jose import jwt
token = jwt.encode(
    {"sub": "3f2a…victim-uuid", "email": "x", "role": "USER",
     "type": "access", "exp": 4102444800},           # year 2100
    "change-me-in-production", algorithm="HS256")
```

| Server step | Result |
|---|---|
| L66 signature check | Passes. The secret matches. |
| L69 `type == "access"` | Passes. The attacker included it. |
| L79 user exists and is active | Passes. The victim is a real user. |
| `exp` | Year 2100, so effectively never expires. |

The attacker now acts as the victim: datasets, chats, connected PostgreSQL sources, queries against the victim's client database within the approved scope, scope changes, deletions. There's no password, no rate limit, nothing in the logs that looks unusual, and it lasts as long as the secret does.

### 3.3 Attack 2: take over the account permanently (needs their UUID)

1. Forge a password-reset token: `{"sub": "<uuid>", "purpose": "password_reset", "exp": …}`.
2. `POST /auth/reset-password` with that token and a new password.
3. `reset_password` sets the password **and** (since Addendum A) `password_proven_at`, then revokes the victim's sessions.
4. The attacker logs in normally and gets real refresh tokens.

**Key point:** fixing the secret later doesn't undo this. The attacker now *knows a password*, which no key rotation invalidates. That's why the runbook (§11) has a remediation branch.

### 3.4 Attack 3: undermine SPEC-01 and Addendum A

A forged `email_verification` token would flip `is_verified` and set `password_proven_at` on any **unverified** row whose UUID the attacker knows, making a squatter's password "proven". Today the attacker can't learn the UUID of their own squatted row, because `/register` doesn't return it, so this is blocked by the same UUID constraint. It's listed because it shows the dependency: **every proof-of-ownership mechanism in SPEC-01 assumes this secret is secret.**

### 3.5 Attack 4: forge OAuth state (no UUID needed, low impact)

An attacker can mint a valid `state` without starting a flow. The callback still validates the return origin against the allowlist (`_validated_frontend_origin`), so the gain is small. It removes the expiry protection on `state`, nothing more.

### 3.6 The amplifier: production that thinks it's dev

If production also runs with `APP_ENV=dev` (Dockerfile L44, §2.3), then turning on `AUTH_DEV_AUTO_VERIFY` (which someone debugging "users can't verify" might well do) gets past SPEC-01's startup guard. Every new signup is auto-verified with its password marked proven, which brings back SPEC-01 Attack C, now **with no UUID needed**.

## 4. Why this is Critical

| Factor | Assessment |
|---|---|
| Severity if the default is live | Complete authentication bypass for any user whose ID is known, plus permanent takeover through password reset. Identity is the root of every other control. |
| Likelihood | Depends on deployment config, which this spec can't see. The design makes the unsafe state the **default** state: no template line, no startup error, and an image that says `dev`. |
| Detectability | None. Forged tokens are byte-for-byte indistinguishable from real ones, and there's no token ID (`jti`) to audit. |
| Reversibility | Rotating the key kills forged tokens but **not** passwords the attacker set via forged reset tokens (§3.3). |
| Blast radius | Connected client databases (as in SPEC-01). |

## 5. The underlying pattern

Both bugs, and SPEC-01's Attack C, share one shape:

> **When configuration is missing, the system falls back to the least safe behaviour and keeps running.**

| Missing thing | Falls back to | Visible? |
|---|---|---|
| `JWT_SECRET_KEY` | A public secret | No |
| `APP_ENV` | `dev` (from code default or Dockerfile) | No |
| Email provider (before Addendum A) | Silently skipping the email | No |
| Auto-verify condition (before SPEC-01) | Auto-verifying in production | No |

The fix is a principle, not a single patch:

> **Missing or unsafe security configuration must either stop the process loudly, or fall back to something that is still secure.**

Addendum A applied this to email delivery. This spec applies it to the signing secret and the environment name.

---

# PART B — The solution

## 6. Invariants

| ID | Invariant |
|---|---|
| INV-11 | The server **never** signs or verifies tokens with a publicly known value, in any environment. |
| INV-12 | Outside `dev`, the server refuses to start unless `JWT_SECRET_KEY` is explicitly set to at least 32 bytes. |
| INV-13 | In `dev` with no secret configured, the server uses a **random per-process** secret: still secure, just not persistent. |
| INV-14 | `APP_ENV` has no default anywhere: not in code, and not in the image. A deployment that doesn't declare its environment fails at startup. |
| INV-15 | A process running on the hosted platform (Render) refuses `APP_ENV=dev`. |
| INV-16 | The secret value never appears in error messages, logs, or `repr(settings)`. |

## 7. The design, line by line

### 7.1 Config fields (`config.py`)

```python
 1  import secrets
 2  from pydantic import PrivateAttr
 3
 4  _KNOWN_WEAK_JWT_SECRETS = frozenset({"change-me-in-production"})
 5  _JWT_MIN_BYTES_NON_DEV = 32
 6  _JWT_MIN_BYTES_DEV = 16
 7
 8  class Settings(BaseSettings):
 9      app_env: Literal["dev", "staging", "prod"]          # no default (INV-14)
10      ...
11      jwt_secret_key: SecretStr | None = None              # was: str with a public default
12      _jwt_secret_ephemeral: bool = PrivateAttr(default=False)
```

| Line | Decision | Why |
|---|---|---|
| 4 | An explicit denylist containing the old default | That string is in the git history forever. Rejecting it **in every environment** (including dev) removes it from circulation: no `.env` file anywhere can keep it alive. The list is deliberately tiny. It isn't a strength check (the length rule and the generator command are); it exists only to remove one known-compromised value. |
| 5 | 32 bytes outside dev | HS256 is HMAC-SHA-256. RFC 7518 §3.2 requires an HMAC key at least as long as the hash output, which is 256 bits = 32 bytes. Below that, the key is the weakest link, not the algorithm. |
| 6 | 16 bytes in dev | Keeps existing test secrets like `"artifact-auth-test-secret"` (25 bytes) working. Dev secrets protect nothing real, but a floor still catches typos like `JWT_SECRET_KEY=x`. |
| 9 | `app_env` has no default | INV-14. Nothing can "be dev" by accident. Local runs get `APP_ENV=dev` from `.env` (`.env.example` already has it) or from `docker-compose.yml` (`${APP_ENV:-dev}`); tests get it from a conftest (§7.6). |
| 11 | `SecretStr | None`, default `None` | `None` means "not configured", which the validator handles explicitly, instead of a fake value that looks configured. `SecretStr` prints as `**********` in `repr`, logs, and tracebacks (INV-16), the same as the connector keys at L137/L141. |
| 12 | A private flag for the ephemeral case | Lets startup log a warning (§7.4) without making it a configurable field: nobody should be able to *set* "ephemeral". |

### 7.2 The validator (`config.py`, next to `_auth_dev_auto_verify_only_in_dev`)

```python
 1  @model_validator(mode="after")
 2  def _jwt_secret_is_safe(self) -> "Settings":
 3      configured = self.jwt_secret_key.get_secret_value() if self.jwt_secret_key else ""
 4
 5      if configured in _KNOWN_WEAK_JWT_SECRETS:
 6          raise ValueError(
 7              "JWT_SECRET_KEY is set to a publicly known placeholder. Generate a new one: "
 8              "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
 9          )
10
11      if self.app_env == "dev":
12          if not configured:
13              self.jwt_secret_key = SecretStr(secrets.token_urlsafe(48))
14              self._jwt_secret_ephemeral = True
15          elif len(configured.encode()) < _JWT_MIN_BYTES_DEV:
16              raise ValueError("JWT_SECRET_KEY must be at least 16 bytes.")
17          return self
18
19      if len(configured.encode()) < _JWT_MIN_BYTES_NON_DEV:
20          raise ValueError(
21              f"JWT_SECRET_KEY must be set to at least {_JWT_MIN_BYTES_NON_DEV} random bytes "
22              f"when APP_ENV={self.app_env}. Generate one: "
23              "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
24          )
25      return self
```

| Line | Decision | Why |
|---|---|---|
| 3 | Unwrap once into a local variable | Every later check uses `configured`. The value is never put into a message. |
| 5–9 | The denylist check runs **first, in every environment** | INV-11. Even a dev box can't run with the public value. The message tells you how to fix it and doesn't echo the value. |
| 7–8 | The error includes the generator command | The person hitting this error is exactly the person who needs the command. Error messages are the documentation people actually read. |
| 11–14 | Dev with nothing configured gets **a random secret, not an error** | This is the "fall back to something still secure" half of the principle (§5). `secrets.token_urlsafe(48)` is 48 random bytes, encoded to 64 characters, from the OS's cryptographic generator. Nobody, including the developer, knows it, so forgery is impossible. The cost: tokens stop working when the process restarts. In dev that's fine (log in again). Refusing to start instead would make every new contributor's first run fail for a value that protects nothing locally. |
| 13 | Assigned inside the validator | `get_settings()` is `@lru_cache(maxsize=1)` (config.py, end of file), so one process builds one `Settings` and gets one stable secret for its lifetime. Within a process, issuing and verifying use the same key. |
| 15–16 | A configured dev secret still has a floor | Catches `JWT_SECRET_KEY=x`. |
| 19–24 | Outside dev: **refuse to start** unless it's 32+ bytes | INV-12. Covers both "not set" (empty string, length 0) and "set but weak" with one rule. Here the fallback *can't* be ephemeral: production runs across restarts (Render free tier restarts after idle), so a per-process secret would log every user out several times a day, and multiple processes would disagree. A startup crash is the correct loud failure. |
| 21 | Names the environment in the message | Tells the deployer *why* it's strict here: "you said prod". |
| — | Length, not entropy | We can't measure entropy from a string. `"aaaa…"` (32 bytes) passes. The real control is the generator command in every message and in the template. The length check catches the common failures (unset, placeholder, short password). |

**Where it runs:** pydantic runs this when `Settings()` is first built. In this codebase that happens in **`alembic/env.py` L21 (`get_settings()`)**, called by `entrypoint.sh` *before* uvicorn starts, and again in `create_app()`. A bad production config therefore fails at the migration step: the deploy is marked failed, and the old instance keeps serving. That's the safest possible place to fail.

### 7.3 The hosted-platform tripwire (`config.py`)

```python
1  @model_validator(mode="after")
2  def _no_dev_on_hosted_platform(self) -> "Settings":
3      if self.app_env == "dev" and os.environ.get("RENDER", "").lower() == "true":
4          raise ValueError(
5              "APP_ENV=dev is not allowed on Render. Set APP_ENV=prod (or staging) "
6              "in the service's environment settings."
7          )
8      return self
```

| Line | Decision | Why |
|---|---|---|
| 3 | Uses the `RENDER` variable | Render sets `RENDER=true` in every service's environment. This is the one signal in the process that comes from the **platform**, not from anything a person typed. It catches the case INV-14 can't: someone *explicitly* sets `APP_ENV=dev` in production (copying `.env.example`, or "temporarily" while debugging). **Verify the variable exists on your service first (§11 step 2)**; if it doesn't, this tripwire is a no-op, and the spec must be revisited rather than shipped as-is. |
| 3 | No escape hatch | An override flag ("allow dev on Render") would recreate the problem one variable later. A preview or staging service on Render should say `staging`, which the rest of the code treats like prod. |
| — | Platform-specific on purpose | Your production runs on Render. If you move hosts, add the new platform's equivalent variable here. That's a one-line change, recorded in §13. |

### 7.4 Startup visibility (`main.py`, `create_app()` after `settings = get_settings()`)

```python
log.info("app.config", app_env=settings.app_env)
if settings.jwt_secret_ephemeral:
    log.warning("auth.jwt_secret.ephemeral",
                detail="JWT_SECRET_KEY not set; using a per-process random key. "
                       "Tokens will not survive a restart. Dev only.")
```

Expose the private flag through a read-only property, `jwt_secret_ephemeral`, on `Settings`.

| Decision | Why |
|---|---|
| Log `app_env` on every start | The single most useful line for diagnosing "why is prod behaving like dev?" Today there's no record of which environment a process thinks it's in. |
| Warn on ephemeral | A developer who wonders why they got logged out after a restart finds the answer in the first screen of logs. |
| Never log the secret, or a hash of it | A hash of a weak secret can be brute-forced offline. The boolean is enough. |

### 7.5 A single accessor for the key (`config.py`) and its call sites

```python
@property
def jwt_signing_key(self) -> str:
    assert self.jwt_secret_key is not None   # guaranteed by _jwt_secret_is_safe
    return self.jwt_secret_key.get_secret_value()
```

Replace **every** `settings.jwt_secret_key` read outside `config.py` with `settings.jwt_signing_key`:

| File | Lines (snapshot) |
|---|---|
| `app/services/auth_service.py` | 82, 118, 151, 158, 182, 194, 291 |
| `app/api/routes/auth_routes.py` | 264, 302 |
| `app/core/dependencies.py` | 66 |
| `tests/unit/test_google_account_linking.py` | 306, 317, 326 |
| `tests/datasets/test_artifact_authorization.py` | 126 (L206 passes a constructor value, which `SecretStr` accepts unchanged) |

| Decision | Why |
|---|---|
| One accessor | The secret is unwrapped in exactly one place. `grep -rn "jwt_secret_key" backend/app` returning only `config.py` becomes an acceptance check (§12). |
| `assert` instead of a check | The validator guarantees non-`None`. An `assert` documents the invariant and fails fast if a future change breaks it. |
| `security.py` unchanged | Its functions already take `secret: str` as a parameter. They don't care where it came from. |

### 7.6 Environment defaults removed

**`Dockerfile` L41–45:** delete `APP_ENV=dev \`. Nothing replaces it.

```dockerfile
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000
```

| Decision | Why |
|---|---|
| Remove, don't change to `prod` | Baking `prod` into the image would make `docker compose up` locally run as prod, the mirror-image mistake. The image must not have an opinion. Local compose already sets `APP_ENV: ${APP_ENV:-dev}` (L11), so local runs are unaffected. |

**`backend/tests/conftest.py` (new, root of the test tree):**

```python
import os

# Tests construct Settings directly. With no default for APP_ENV (SPEC-02),
# declare the test environment explicitly, once, for the whole suite.
os.environ.setdefault("APP_ENV", "dev")
os.environ.pop("RENDER", None)
```

| Line | Why |
|---|---|
| `setdefault("APP_ENV", "dev")` | 31 test files build `Settings(...)` without `app_env`. This keeps them all working without editing 31 files, and `setdefault` respects an explicit override when a test runner sets one. |
| `pop("RENDER", None)` | If the suite ever runs in a Render shell, the tripwire would otherwise fail every test. |
| Module top level, not a fixture | `config.py` calls `load_dotenv(override=False)` **at import time**. The variable must be set before any test module imports `app.core.config`, and a root `conftest.py` is imported first. |

**`.env.example`:** replace the top block with:

```dotenv
# ─── Server ──────────────────────────────────────────────────────────────
# LOCAL TEMPLATE ONLY. Deployed services set APP_ENV=prod (or staging) in the
# platform's environment settings. There is no default: the server will not
# start without it.
APP_ENV=dev

# Signs access, verification, password-reset and OAuth-state tokens.
# Required (32+ bytes) when APP_ENV is staging/prod; the server refuses to start
# otherwise. Leave empty in local dev to use a random per-process key (you will
# be logged out on restart). Generate:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET_KEY=
```

The `AUTH_DEV_AUTO_VERIFY` comment block from SPEC-01 stays as is.

## 8. Why this design, and not the alternatives

| Alternative | Verdict | Reasoning |
|---|---|---|
| **Keep the default, add a warning log** | Rejected | A warning nobody reads, on a server that's fully exploitable. This is the "fail-open, but visible" option, and it isn't visible either: nobody watches startup logs on a healthy-looking service. |
| **Require `JWT_SECRET_KEY` in every environment, including dev** | Rejected | Every new contributor's first run, and every one of the 31 test files constructing `Settings`, would fail for a value that protects nothing locally. The ephemeral dev key is equally secure (nobody knows it) and costs nothing. |
| **Ephemeral key in every environment** (never require configuration) | Rejected | It's secure, but in production every restart logs everyone out (Render free tier restarts after idle), and any second process disagrees on the key. Correct in dev, broken for real users. |
| **Make `app_env` required, but keep `APP_ENV=dev` in the Dockerfile** | Rejected | The "required" check never fires, because the image always supplies a value (§2.3). It just moves the wrong default from Python to Docker. |
| **Bake `APP_ENV=prod` into the Dockerfile** | Rejected | Local `docker compose` runs would claim prod unless overridden. Wrong in the other direction. Images shouldn't encode environments. |
| **Keep the `RENDER` tripwire but add an override flag** | Rejected | A flag to turn off the safety check is the next misconfiguration. Non-local Render services use `staging`. |
| **Separate secrets per token purpose** (access vs reset/verify/state) | Deferred | The `type`/`purpose` checks (§2.5) already prevent one token kind being used as another. Separate secrets help only if *one* secret leaks and not the others. In this deployment they'd all live side by side in the same environment-variable store, so they'd almost certainly leak together. That's real operational cost for little gain at MVP stage. Revisit with a proper secret manager. |
| **Key rotation with `kid` and a previous-key ring** (like the connector keyring) | Deferred | Lets you rotate without logging users out. Today rotation costs one forced re-login (access tokens are 30 minutes; refresh tokens are unaffected, §1.2). Worth building once there are paying users; not needed to close this bug. |
| **Switch to RS256** (asymmetric keys) | Rejected for MVP | Useful when *other* services must verify tokens without being able to mint them. NeuroNova has one verifier (itself), so HS256 with a strong secret is the right tool. |

## 9. Behaviour matrix and tests

**File:** `backend/tests/unit/test_settings_security.py` (new).

**Isolation rules for every test:**
- Build settings as `Settings(_env_file=None, database_url="sqlite://", api_key="test-key-123", ...)`. `_env_file=None` stops a developer's `backend/.env` from leaking in.
- `config.py` runs `load_dotenv(override=False)` at import, which copies `.env` into `os.environ`. So every test must also `monkeypatch.delenv("JWT_SECRET_KEY", raising=False)`, `monkeypatch.delenv("APP_ENV", raising=False)` and `monkeypatch.delenv("RENDER", raising=False)`, then set exactly what it needs.
- Use `pytest.raises(ValidationError)` (pydantic wraps `ValueError` from validators).

### 9.1 Exploit tests (write first, must fail on current code)

| ID | Test | On current code |
|---|---|---|
| E1 | `test_default_secret_cannot_mint_valid_tokens`: build `Settings(app_env="prod")` with **no** `JWT_SECRET_KEY`; expect `ValidationError`. | Fails: settings build fine with the public default. To show the real impact, the test's docstring records that on unfixed code `decode_access_token(jwt.encode({...,"type":"access"}, "change-me-in-production"), settings.jwt_secret_key)` succeeds. |
| E2 | `test_dev_on_render_refused`: `RENDER=true`, `app_env="dev"`; expect `ValidationError`. | Fails: no tripwire exists. |

### 9.2 Matrix

| # | `APP_ENV` | `JWT_SECRET_KEY` | `RENDER` | Expected |
|---|---|---|---|---|
| T1 | `prod` | unset | — | `ValidationError` (E1) |
| T2 | `prod` | `change-me-in-production` | — | `ValidationError` (denylist) |
| T3 | `prod` | 31 bytes | — | `ValidationError` |
| T4 | `prod` | `secrets.token_urlsafe(48)` | — | Builds; `jwt_signing_key` equals it; `jwt_secret_ephemeral is False` |
| T5 | `staging` | unset | — | `ValidationError` (staging is treated like prod) |
| T6 | `dev` | unset | — | Builds; key ≥ 64 chars; `jwt_secret_ephemeral is True`; **two separate `Settings()` get different keys** |
| T7 | `dev` | `change-me-in-production` | — | `ValidationError` (denylist applies in dev too) |
| T8 | `dev` | `"artifact-auth-test-secret"` (25 bytes) | — | Builds (existing test convention keeps working) |
| T9 | `prod` | `"leakcheck-short"` (15 bytes) | — | `ValidationError`, and `"leakcheck-short"` **does not appear** in `str(exc)` (INV-16) |
| T10 | unset | any | — | `ValidationError` (no default, INV-14) |
| T11 | `dev` | any | `true` | `ValidationError` (E2) |
| T12 | `prod` | valid | `true` | Builds |
| T13 | `prod` | valid | — | `repr(settings)` doesn't contain the secret |
| T14 | `prod` | key A | — | A token minted with `"change-me-in-production"` is **rejected** by `decode_access_token(token, settings.jwt_signing_key)` (raises `JWTError`) |

**Existing suite:** must pass unchanged apart from the call-site renames in §7.5. If any test fails because it relied on the old default secret (for example, minting a token with one `Settings` and verifying with another), fix the test by passing an explicit `jwt_secret_key`. Don't weaken the validator.

## 10. Implementation order for Claude Code

1. **Create** `backend/tests/unit/test_settings_security.py` with E1 and E2. Run them and confirm both fail on the current code. **Stop and report if either passes.**
2. **Create** `backend/tests/conftest.py` (§7.6). Run the full suite; it must still be green (nothing has changed yet except setting `APP_ENV`, which matches today's default).
3. **Edit** `backend/app/core/config.py`:
   - imports (`secrets`, `PrivateAttr`);
   - constants (§7.1 L4–6);
   - `app_env` without a default;
   - `jwt_secret_key: SecretStr | None = None`, `_jwt_secret_ephemeral`;
   - `_jwt_secret_is_safe` (§7.2) and `_no_dev_on_hosted_platform` (§7.3);
   - the `jwt_signing_key` and `jwt_secret_ephemeral` properties.
4. **Rename** every call site listed in §7.5 to `settings.jwt_signing_key`.
5. **Edit** `backend/main.py`: the startup logs (§7.4).
6. **Edit** `Dockerfile`: remove `APP_ENV=dev` (§7.6).
7. **Edit** `.env.example`: the top block (§7.6).
8. **Add** T1–T14. Run the full suite on a fresh virtualenv from `backend/requirements.txt`.
9. **Local smoke test:**
   - `APP_ENV=prod` with no secret, then `alembic upgrade head`: it must fail with the generator hint.
   - `APP_ENV=dev`, no secret, start the server: the `auth.jwt_secret.ephemeral` warning appears and login works.
10. **Report:** a diff summary, test output, E1/E2 before and after, and the smoke-test output (with no secrets in it).

**Files touched:** `backend/app/core/config.py`, `backend/app/services/auth_service.py`, `backend/app/api/routes/auth_routes.py`, `backend/app/core/dependencies.py`, `backend/main.py`, `Dockerfile`, `.env.example`, `backend/tests/conftest.py` (new), `backend/tests/unit/test_settings_security.py` (new), `backend/tests/unit/test_google_account_linking.py`, `backend/tests/datasets/test_artifact_authorization.py`. Nothing else.

## 11. Deploy runbook (Siddhant, by hand, in this order)

**Do steps 1–4 before merging.** After this change, a production service that isn't configured correctly will **refuse to deploy**. That's intended, but it should fail on purpose, not by surprise.

| # | Step | Why |
|---|---|---|
| 1 | **Find out whether production uses the default secret.** Safe method: copy *your own* access token from the browser (DevTools → Application → Local Storage), then run locally: `python -c "from jose import jwt; print(jwt.decode('<token>', 'change-me-in-production', algorithms=['HS256']))"`. If it prints your claims, **production is signing with the public default.** Also look at the Render dashboard → Environment for `JWT_SECRET_KEY` and `APP_ENV`. | This verifies your own token offline. You never send a forged token to production. The answer decides step 7. |
| 2 | **Confirm `RENDER=true` exists** on the service (Render dashboard → Shell: `echo $RENDER`). | §7.3 depends on it. If it isn't there, stop and revise §7.3 before merging. |
| 3 | Generate a secret: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Set it as `JWT_SECRET_KEY` on Render. **Don't** put it in any file, chat, or commit. | The new code refuses to start without it. |
| 4 | Set `APP_ENV=prod` on Render explicitly, and confirm `AUTH_DEV_AUTO_VERIFY` is **not** set. | The image no longer supplies `APP_ENV`, so without this the deploy fails, which is better than running as dev. |
| 5 | Merge and deploy. The logs should show `app.config app_env=prod` and **no** `auth.jwt_secret.ephemeral`. | Confirms the config was read as intended. |
| 6 | Expect: all existing access tokens (≤30 min) are invalid; pending verification and reset links and in-flight Google logins fail and must be retried. Refresh tokens are **unaffected** (§1.2), so if the frontend retries with the refresh token on a 401, users barely notice. Otherwise they log in once. | Normal effect of changing the signing key. |
| 7 | **Only if step 1 showed the default was live:** treat it as a possible compromise. A forged password-reset token leaves the attacker holding a real password that rotation doesn't revoke (§3.3). Re-run Addendum A's forced reset for everyone: one SQL statement that sets `password_proven_at = NULL` for all users with a password and revokes all refresh tokens. That forces every password user through a reset email chosen by the inbox owner. Write the statement in the PR and have it reviewed before running. Google-only users are unaffected (they have no password to have been reset). | You can't tell forged activity from real activity after the fact (no `jti`, and `forgot_password` doesn't log requests). The forced reset is the only remediation that doesn't depend on detecting the attack. |

## 12. Acceptance criteria

- [ ] E1 and E2 fail on the pre-change code (evidence in the PR) and pass after.
- [ ] T1–T14 pass. The full suite passes on a fresh virtualenv.
- [ ] `grep -rn "change-me-in-production" backend/app` returns only the denylist constant.
- [ ] `grep -rn "jwt_secret_key" backend/app` returns only `backend/app/core/config.py`.
- [ ] `grep -n "APP_ENV" Dockerfile` returns nothing.
- [ ] `app_env` has no default in `config.py`.
- [ ] Smoke test: `APP_ENV=prod` with no secret makes `alembic upgrade head` fail with the generator hint, and the output contains no secret.
- [ ] Runbook steps 1–6 completed; step 7 completed or explicitly marked not needed, with the step 1 result recorded in the PR (whether the default was live; **never** the secret itself).

## 13. Out of scope / follow-ups

- **Legacy `API_KEY` in the frontend bundle** (`VITE_API_KEY`, and `API_KEY=dev-local-key-change-me` in `.env.example` and `docker-compose.yml`). This is the same fail-open pattern applied to a different credential, and it's the next spec.
- **Logging `forgot_password` requests** with `user_id` (never the token). That would make §11 step 7 a targeted audit instead of a blanket reset next time. Small; bundle it with the reset-token-reuse fix.
- **`jti` claims and a revocation list**, for the ability to revoke individual access tokens.
- **Key rotation with `kid`** and a previous-key ring (§8).
- **Moving hosts:** add the new platform's environment marker to `_no_dev_on_hosted_platform`.
- **Production startup checks for other required settings** (email provider, Google OAuth secret when the client ID is set, connector keys when connectors are enabled). Same pattern, applied one setting at a time as each becomes production-critical.