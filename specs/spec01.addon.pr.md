# SPEC-01 · Addendum A — Legacy accounts, the local signup dead-end, and test reproducibility

| Field | Value |
|---|---|
| Status | Proposed. **Ships in the same PR as SPEC-01** (commit `d894b11` and follow-ups) |
| Severity | P1 Critical (A1), P2 (A2, A3) |
| Trigger | Greptile review of the SPEC-01 PR: confidence 3/5, "not mergeable as a complete takeover fix" |
| Amends | SPEC-01 §6.1 (Rule 2 condition), §6.4 (Rule 4), §8 (matrix), §9 (tests), §11 (files) |
| Audience | Siddhant (owner, reviewer) and Claude Code (implementer) |

---

## 0. Why this is an addendum and not a new spec

The three findings are gaps *in the SPEC-01 fix itself*, not new bugs:

| Finding | Relationship to SPEC-01 |
|---|---|
| **A1 (P1).** Rows auto-verified *before* the fix keep a password that was never proven. | SPEC-01 §3 already identified this (Attack C, "a row already verified is not protected by Rule 2"). It closed the hole for **future** rows only. The **existing** rows were left to a log search (§6.4 "Operational check"), which is not a fix. Greptile is right. |
| **A2 (P2).** Local signup can no longer complete. | A regression **introduced by** SPEC-01 Rule 4. |
| **A3 (P2).** `aiosqlite` is not declared. | SPEC-01's tests depend on it. (The existing `tests/catalog/*` tests already did too. This is a pre-existing gap that the security tests now depend on.) |

**Recommendation: amend the open PR rather than merging and following up.** A2 is a regression the PR creates, and A1 changes the Rule 2 condition the PR just wrote. Merging and then patching means shipping a known regression and rewriting the same lines twice.

For the record, merging the PR as-is would *not* make production less safe than `main`. It's strictly better. But it can't be called "the takeover fix" until A1 ships.

---

# PART A — The problems

## A1. Rows auto-verified in the past are still exploitable (P1)

### A1.1 Why the SPEC-01 fix misses them, line by line

The SPEC-01 Google branch as merged in the PR:

```python
24  if not user.is_verified:
25      await _reclaim_unproven_account(db, user, name=name, avatar_url=avatar_url)
26  elif avatar_url and not user.avatar_url:
27      user.avatar_url = avatar_url
28  user.google_id = google_id
```

| Line | Assumption | Holds for new rows? | Holds for legacy rows? |
|---|---|---|---|
| 24 | `is_verified == True` means "the person who set this password proved the inbox". | ✅ After Rule 4, `True` can only come from `verify_email` (the inbox owner clicked) or Google. | ❌ Before Rule 4, `register_user` set `is_verified=True` for **every** signup when `APP_ENV` was unset or `dev` and no email provider was configured. For those rows the flag records *nothing*. |
| 26–28 | A verified row's password is legitimate, so keep it and link. | ✅ | ❌ The password may be the squatter's. Linking puts the real owner into the squatter's account, which is exactly Attack C. |

**The root cause:** `is_verified` is a single boolean answering two different questions:

1. "Has someone proven they own this inbox?"
2. "Was the **current password** set by someone who proved they own this inbox?"

For rows created through the email-link flow, the answers happen to be the same. For dev-auto-verified rows, both answers are "no", but the flag says `True`. **The flag cannot be repaired after the fact, because the database never recorded *how* a row became verified.** Every SPEC-01 rule keyed on `is_verified` inherits that blindness.

### A1.2 Worked example

The cast from SPEC-01: Priya is the victim (`priya@acme-logistics.com`) and Mallory is the attacker. Here, Mallory's step happens **last month**, while production was running with the default `APP_ENV` and no Resend key.

| When | Event | Row state |
|---|---|---|
| Last month | Mallory registers `priya@acme-logistics.com` / `Mall0ry!pass`. Old `register_user` auto-verifies the row. | `hashed_password=H(Mall0ry!pass)`, `is_verified=true`, `google_id=null` |
| Last month | Mallory logs in and holds a 7-day refresh token. She can keep renewing it by logging in again. | + `refresh_tokens` row, `revoked=false` |
| Today | The SPEC-01 PR deploys. The migration-free fix changes **nothing** about this row. | unchanged |
| Tomorrow | Priya signs in with Google. Line 24 is `False` (row is verified), so it goes to the `elif` and **links Google to Mallory's account**. | `google_id=G-PRIYA`, password still Mallory's |
| Tomorrow | Priya connects Acme's production database. Mallory logs in with `Mall0ry!pass` and has full access. | — |

This is Attack A from SPEC-01 with the waiting step removed. The SPEC-01 test matrix never covered it, because every M-row seeds a *verified* row as if its password were trustworthy (M3).

### A1.3 Why "search the logs" (SPEC-01 §6.4) is not a fix

| Problem | Consequence |
|---|---|
| Hosted log retention is short (days to weeks on most PaaS plans). | **Absence of evidence is not evidence of absence.** An empty log search proves nothing about last month. |
| The event logs the email, not the provenance on the row. | Even a hit requires a manual, per-row decision. That isn't repeatable, testable, or enforced by code. |
| It's a one-time human step. | Nothing stops a mistake being repeated later. |

**Conclusion:** provenance has to live **in the database**, recorded at the moment trust is established, and legacy rows whose provenance is unknown must be treated as **unproven** (fail closed).

### A1.4 Why this matters beyond Google linking

A squatted account with `is_verified=True` is also a forward liability. Any future feature that trusts a verified email inherits the lie: team invites by email, "join your company's workspace by domain", sharing an artifact with `priya@acme-logistics.com`. Each of those would route Priya's access into Mallory's account. Fixing only the Google-link path would leave a time bomb for whoever builds those features next.

## A2. Local signup is a dead end (P2)

With SPEC-01 applied and a fresh `.env` copied from `.env.example`:

| Step | Code | Effect |
|---|---|---|
| 1 | `AUTH_DEV_AUTO_VERIFY` is commented out, so it's `False`. | — |
| 2 | `register_user` sees `dev_auto_verify = False`. | Row created with `is_verified=False`. |
| 3 | `email_service._send` sees no Resend key and no `smtp_host`. | Logs `email.skipped` and **returns normally**. |
| 4 | `POST /register` returns **201**. | The developer thinks it worked. |
| 5 | `POST /login` | 401 "email not verified". The account is permanently unusable. |

The real defect is in step 3: **a request whose success depends on an email being delivered reports success when no email can be delivered.** That's a fail-silent path. It hits developers today, and in production it would hit real users if the Resend key were ever missing or rotated incorrectly.

**Why we must *not* fix this by uncommenting the flag in `.env.example`:** `APP_ENV` defaults to `dev`. If someone seeds a production environment from `.env.example` without setting `APP_ENV=prod`, the Rule 4 validator passes (flag on, env is dev), and **Attack C is back**. A config template must never contain a security bypass that's switched on.

## A3. Security regression tests are not reproducible (P2)

`tests/unit/test_google_account_linking.py` builds `create_async_engine("sqlite+aiosqlite:///:memory:")`. `backend/requirements.txt` declares `pytest` and `pytest-asyncio` but **not** `aiosqlite`. On a fresh install, fixture setup fails, and the tests that prove the takeover is fixed don't run. A security regression test that can't run on a clean checkout doesn't protect anything.

---

# PART B — The solution

## B0. New invariants

| ID | Invariant | Fixes |
|---|---|---|
| INV-6 | The database records **when a password was proven** (`users.password_proven_at`). A password is trusted only if that timestamp is set. | A1 |
| INV-7 | Rows whose password provenance is unknown (all pre-migration password rows) are **unproven** until the inbox owner sets a password through an emailed reset link. | A1 |
| INV-8 | No unproven password grants a session: not through login, not through Google linking, and not through a refresh token issued before this change. | A1 |
| INV-9 | An operation that depends on email delivery **fails loudly** (no row created, clear error) when no delivery path exists. | A2 |
| INV-10 | Every test dependency is declared in the manifest the project installs from. | A3 |

## B1. Schema: record password provenance

### B1.1 Model (`backend/app/db/models/user.py`)

```python
password_proven_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

| Decision | Why |
|---|---|
| A timestamp, not a boolean | It records *when*, which helps audits and lets a future policy re-prove passwords older than N days. A boolean would recreate the `is_verified` problem: a fact with no history. |
| Separate from `is_verified` | Splits the two questions from A1.1. `is_verified` keeps meaning "the inbox was proven at some point"; `password_proven_at` answers "the **current** password was set by the inbox owner". |
| Nullable, no default | `NULL` means "not proven". Fail closed: any path that forgets to set it produces an untrusted password, never a trusted one. |

### B1.2 Who sets it (the complete list)

| Code path | Sets `password_proven_at`? | Reasoning |
|---|---|---|
| `register_user` (normal) | No (`NULL`) | The registrant has proven nothing yet. |
| `register_user` with dev auto-verify (Rule 4 opt-in) | Yes, `now()` | Explicit, dev-only opt-in. Without this, Rule B4 would lock every local dev account at login and local development breaks again (A2 in a new form). The validator still forbids this outside `dev`. |
| `verify_email` **transition** from `is_verified=False` to `True` | Yes, `now()`, if `hashed_password` is not `NULL` | The inbox owner clicked the link for an account whose password was set at registration. Only on the *transition*. An already-verified row (including every legacy auto-verified row) gains nothing from `verify_email`, so the old flow can't be used to bless a legacy password. |
| `reset_password` | Yes, `now()` | The strongest proof: the inbox owner received the link **and chose the new password themselves**. A squatter's password is destroyed by construction. |
| `_reclaim_unproven_account` | Not applicable; it sets `hashed_password=None` | No password, so nothing to prove. |
| Google new-user branch | Not applicable; no password | — |

**Known residual (accepted):** if Mallory registers Priya's email and *Priya* clicks the verification link despite never signing up, `verify_email` marks Mallory's password proven. Every email-verification system has this social-engineering residual. Mitigate it in the email copy (§B5). It needs a victim action against an explicit warning, unlike A1, which needed none.

## B2. Migration `0016_password_provenance`

```python
revision = "0016_password_provenance"
down_revision = "0015_stage7_source_operations"

def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_proven_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Fail closed: provenance of every existing password is unknown (A1.3).
    # No backfill — every pre-existing password row is NULL = unproven.

    # INV-8: sessions minted from unproven passwords must not outlive this deploy.
    op.execute(
        """
        UPDATE refresh_tokens
           SET revoked = true
         WHERE revoked = false
           AND user_id IN (SELECT id FROM users WHERE hashed_password IS NOT NULL)
        """
    )

def downgrade() -> None:
    op.drop_column("users", "password_proven_at")
    # Revoked refresh tokens are NOT restored; revocation is the safe state.
```

| Line / decision | Why |
|---|---|
| No backfill | We can't tell link-verified rows from dev-auto-verified rows (A1.1). Backfilling "proven" for any of them would trust a guess. Every legacy password row gets one forced reset, and that is the whole cost of being certain. |
| Revoke refresh tokens for **password** users only | Mallory's live session from last month dies at deploy. Google-only users (`hashed_password IS NULL`) were never exposed to A1 and keep their sessions. |
| Revoke users whose `hashed_password` is not null, even if they're also Google-linked | A row with both could be a legacy squatter row that a Google user was already linked into (A1.2 may have already happened). Revoking is cheap: the Google user signs in with Google again. |
| Irreversible downgrade note | Un-revoking tokens would undo the security effect. The downgrade restores the schema, not the risk. |

**What this costs users:** every existing password user is logged out once, and their next password login asks them to set a password via email (§B4). Google users are unaffected. At MVP scale that's a handful of people and one email each, which is acceptable for removing an unbounded takeover risk.

**Access tokens:** the migration can't touch these (they're stateless JWTs, valid for up to 30 minutes). For an instant cut-over, rotate `JWT_SECRET_KEY` during the same deploy (see the runbook, §B7). Refresh tokens are opaque database rows, not JWTs, so Google users re-mint their access tokens silently on the next refresh.

## B3. Google linking: Rule 2 keyed on provenance (amends SPEC-01 §6.1 L24)

```python
24  if not user.is_verified or (
25      user.hashed_password is not None and user.password_proven_at is None
26  ):
27      # Unproven row OR proven inbox with an unproven password (legacy auto-verify).
28      await _reclaim_unproven_account(db, user, name=name, avatar_url=avatar_url)
29  elif avatar_url and not user.avatar_url:
30      user.avatar_url = avatar_url
31  user.google_id = google_id
```

| Line | Why |
|---|---|
| 24 | Keeps SPEC-01's original case: unverified rows are always reclaimed. |
| 25 | **The A1 fix.** A verified row whose password has no recorded proof is treated exactly like an unverified one. Re-run A1.2: tomorrow, Priya's Google sign-in now **reclaims** the row. Mallory's password is set to `NULL`, her tokens are revoked, and her login at the last step fails at `not user.hashed_password`. |
| 25 | `hashed_password is not None` guard | A Google-only row has no password, so there's nothing to reclaim. Don't overwrite its name or avatar. |
| 28 | Reuses the SPEC-01 helper unchanged | Same cleanup semantics: password, name, avatar, tokens, then verified. One reclaim path, one set of tests. |
| 29–31 | Unchanged | A verified row with a **proven** password still links and keeps its password (INV-5 from SPEC-01). |

**Residual risk: data planted in a reclaimed legacy row.** If Mallory created sources, datasets, or chats while squatting, Priya inherits them after the reclaim. The realistic abuse is a source pointing at a database *Mallory* controls, so that Priya's questions show up in Mallory's query logs. This needs Priya to use a source she didn't create. The runbook audit (§B7) lists every unproven row that owns data, so these can be reviewed by hand before anyone reclaims them. Automated quarantine is out of scope for the MVP.

## B4. Login: unproven passwords require a reset (INV-7, INV-8)

### B4.1 New error (`backend/app/exceptions/custom_exceptions.py`)

```python
class PasswordResetRequiredError(UnauthorizedError):
    code = "password_reset_required"
```

### B4.2 `login_user` (after the existing `is_verified` check)

```python
84  if not user or not user.hashed_password or not verify_password(password, user.hashed_password):
85      raise UnauthorizedError("invalid credentials")
86  if not user.is_active:
87      raise UnauthorizedError("account disabled")
88  if not user.is_verified:
89      raise UnauthorizedError("email not verified — check your inbox")
90  if user.password_proven_at is None:
91      raise PasswordResetRequiredError(
92          "For security, please set a new password using “Forgot password”."
93      )
```

| Line | Decision | Why |
|---|---|---|
| 90 | Checked **after** the password check (L84) | Only someone holding the correct password sees this message, so it leaks nothing to a prober about whether the account exists. That keeps the enumeration property of the existing code. |
| 90 | Checked after `is_verified` | Unverified accounts keep their existing, more specific message. |
| 91 | Subclass of `UnauthorizedError` | It still maps to 401, still caught by every existing `UnauthorizedError` handler, and the `code` lets the frontend show a "Reset password" button later. |
| 92 | Tell the user to use "Forgot password"; **don't auto-send an email** | Auto-sending on every correct-password login would let Mallory trigger a stream of reset emails to Priya's inbox, rate-limited only by the login limiter (A spam and social-engineering vector: Priya might click one). Making the user start the reset keeps the email strictly user-initiated. It also needs no new state (a "last sent at" column) and no new delivery path. |

**Why a reset and not "click to confirm":** a confirm link would let the inbox owner bless **whatever password is currently on the row**, which could be Mallory's (the residual in §B1.2, but now Mallory can *trigger* it at will by trying to log in). A reset has Priya choose the password herself, so Mallory's password is gone regardless of what Priya clicks.

### B4.3 Refresh tokens

`refresh_access_token` needs no change. Every refresh token that predates this deploy and belongs to a password user is revoked by the migration (§B2). Every refresh token issued afterwards goes through `login_user` (now gated by L90), through Google (gated by Rule 2), or is issued at reset time (none are). So INV-8 holds without touching the refresh path.

## B5. Fail loudly when email can't be delivered (fixes A2, INV-9)

### B5.1 Helper (`backend/app/services/email_service.py`)

```python
def delivery_configured(settings: Settings) -> bool:
    return bool(settings.resend_api_key or settings.smtp_host)
```

It mirrors `_send`'s own branching exactly (Resend first, then SMTP), so the precondition and the actual send can't disagree.

### B5.2 New error

```python
class EmailDeliveryUnavailableError(AppError):
    status_code = 503
    code = "email_delivery_unavailable"
```

### B5.3 `register_user` (before creating the row)

```python
if not dev_auto_verify and not email_service.delivery_configured(settings):
    if settings.app_env == "dev":
        raise EmailDeliveryUnavailableError(
            "Email delivery is not configured. For local development set "
            "AUTH_DEV_AUTO_VERIFY=true in backend .env (allowed only with APP_ENV=dev)."
        )
    log.error("auth.register.email_delivery_unavailable")
    raise EmailDeliveryUnavailableError("Registration is temporarily unavailable.")
```

| Decision | Why |
|---|---|
| Checked **before** the row is created | A failed registration leaves no unusable row behind. Fixing A2 step 2 rather than step 5 means no dead accounts and no `email already registered` conflicts when the developer retries after fixing their `.env`. |
| A detailed message only in `dev` | The developer gets the exact fix in the error. Outside dev, the message doesn't advertise infrastructure configuration to the public. |
| `log.error` outside dev | A production registration hitting this path is an incident (the Resend key is missing or broken). It should page someone, not degrade silently. |
| 503, not 500 | The request was valid; a dependency is unavailable. Clients and monitoring treat 503 as "retry later or alert ops". |

### B5.4 `forgot_password`

Apply the same precondition. Today it calls `send_password_reset_email`, which silently skips when unconfigured, and the route returns **202** ("we sent you an email") when nothing was sent. With B4 in place, a forced reset depends on this path working, so it must fail loudly too: raise `EmailDeliveryUnavailableError` with the same dev and non-dev message split. Place the check **before** the user lookup, so the error doesn't depend on whether the email exists and the existing anti-enumeration behaviour (`if not user: return`) is unchanged.

### B5.5 Email copy

In `_verification_html`, add one sentence: *"If you didn't create a NeuroNova account, ignore this email. Don't click the link."* This narrows the residual from §B1.2. It's a copy change only.

### B5.6 `.env.example` stays as SPEC-01 left it

The flag stays **commented out** (see A2 for why). The error message from B5.3 is now the documentation a developer actually reads at the moment they need it.

## B6. Declare the test dependency (fixes A3, INV-10)

Add to `backend/requirements.txt`, next to `pytest` and `pytest-asyncio`:

```
aiosqlite==<version installed in your dev env>  # test-only: in-memory SQLite for unit tests
```

| Decision | Why |
|---|---|
| Pin the version you already run (`pip show aiosqlite`) | Pinning a version the suite has never run against risks a red build unrelated to this change. The repo pins every other dependency exactly. |
| `requirements.txt`, not a new `requirements-dev.txt` | `pytest` is already declared there, so that's the existing convention. Splitting dev and prod dependencies is a worthwhile cleanup, but it's a separate change that touches the Dockerfile and CI. Out of scope. |
| It also fixes `tests/catalog/*` | Those tests already used `aiosqlite` without declaring it. Worth noting in the PR so reviewers know the blast radius is positive. |

## B7. Deploy runbook (one-time, in this order)

| # | Step | Why this order |
|---|---|---|
| 1 | **Confirm production email delivery works**: Resend key set, send a test reset to yourself. | After deploy, every legacy password user's only way back in is the reset email. Deploying without delivery locks them out (and B5.4 now makes that visible as a 503, rather than a silent 202). |
| 2 | Confirm `APP_ENV=prod` is set and `AUTH_DEV_AUTO_VERIFY` is **unset** in production. | Rule 4 refuses startup if the flag is set outside dev, but an unset `APP_ENV` still means `dev`. Check it explicitly. |
| 3 | Deploy the PR. The migration runs `0016`. | Revokes password-user refresh tokens. |
| 4 | **Same deploy:** rotate `JWT_SECRET_KEY`. | Kills access tokens (≤30 min) immediately, so Mallory has no tail window. It also invalidates outstanding verification and reset links, which is fine; users request new ones. Google users re-mint silently via their refresh tokens. |
| 5 | Run the audit query below and review every row that owns data. | The residual from §B3. |
| 6 | Tell existing password users they'll need to reset once. | Turns an unexplained login error into an expected step. |

**Audit query (read-only):**

```sql
SELECT u.id, u.email, u.google_id IS NOT NULL AS google_linked,
       (SELECT count(*) FROM data_sources s        WHERE s.owner_user_id = u.id) AS sources,
       (SELECT count(*) FROM dataset_records d     WHERE d.user_id       = u.id) AS datasets,
       (SELECT count(*) FROM conversation_sessions c WHERE c.user_id     = u.id) AS chats
  FROM users u
 WHERE u.hashed_password IS NOT NULL
   AND u.password_proven_at IS NULL
 ORDER BY sources DESC, datasets DESC;
```

Pay special attention to any row where `google_linked = true` **and** there's data: that's the A1.2 shape, where a Google user may already be living inside a squatter's account. Contact the Google user, and check the source connection details belong to them.

---

# PART C — Why this design

| Alternative | Verdict | Reasoning |
|---|---|---|
| **Search logs and fix only the rows found** (SPEC-01 §6.4) | Rejected | Absence of log lines doesn't prove absence of events (A1.3). Not enforced by code, not testable. |
| **Fix only the Google-link path** (Greptile's minimal reading) | Rejected | It leaves Mallory's existing password session alive until Priya happens to arrive, and leaves `is_verified` lying for every future feature that trusts a verified email (A1.4). |
| **Set `is_verified=false` on all legacy password rows** | Rejected | It reuses the overloaded boolean (A1.1) instead of splitting it. Re-verifying through the verification link has the "victim clicks, squatter's password blessed" residual, now triggerable on demand. And there's no resend-verification endpoint, so users would be stuck. |
| **Backfill `password_proven_at` for rows we believe are clean** (for example, created after Resend was configured) | Rejected | Needs reliable knowledge of the deployment config at each row's creation time. That's the same guess A1.3 rejects. The forced reset costs one email per user. |
| **Auto-send a reset email on unproven login** | Rejected | Lets an attacker who holds a squatted password trigger emails to the victim on demand (§B4.2). |
| **Delete all unproven legacy rows** | Rejected | Destroys legitimate users' data (sources, datasets, chats) to fix a credential problem. Credentials are the problem, so credentials are what gets reset. |
| **Uncomment `AUTH_DEV_AUTO_VERIFY=true` in `.env.example`** | Rejected | Brings back Attack C for any production environment seeded from the template without `APP_ENV=prod` (A2). |

---

# PART D — Tests, acceptance, implementation

## D1. Updated behaviour matrix (new and changed rows)

| # | Setup | Action | Expected |
|---|---|---|---|
| M3 *(changed)* | Verified row, password **proven** (`password_proven_at` set) | Google sign-in | Linked; password still works |
| M14 | Verified row, password **unproven** (the A1.2 legacy shape) | Google sign-in | **Reclaimed**: password `NULL`, tokens revoked, name and avatar from Google; the old password's login returns 401 |
| M15 | Verified Google-only row (`hashed_password NULL`, `google_id NULL`) | Google sign-in | Linked, **not** reclaimed (name preserved) |
| M16 | Verified row, unproven password | `login_user` with the correct password | `PasswordResetRequiredError` (401, code `password_reset_required`) |
| M17 | Verified row, unproven password | `login_user` with a **wrong** password | Plain `UnauthorizedError("invalid credentials")`, no reset hint (no enumeration) |
| M18 | Unproven row | `reset_password` | `password_proven_at` set; the new password logs in |
| M19 | Unverified row with a password | `verify_email` | `is_verified=True` **and** `password_proven_at` set |
| M20 | **Already-verified** row, unproven password | `verify_email` with a valid token | `password_proven_at` stays `NULL` (the legacy password can't be blessed via verify) |
| M13 *(changed)* | Dev opt-in on | `register_user` | Verified **and** `password_proven_at` set; `login_user` succeeds |
| M21 | No email provider, opt-in off, `app_env="dev"` | `register_user` | `EmailDeliveryUnavailableError` (503) with the dev hint; **no row created** |
| M22 | No email provider, `app_env="prod"` | `register_user` | `EmailDeliveryUnavailableError` with the generic message; no row created |
| M23 | No email provider | `forgot_password` for an existing **and** a non-existing email | Both raise `EmailDeliveryUnavailableError` (no enumeration via different errors) |
| M12 *(changed)* | Opt-in off, `app_env="dev"`, **email provider configured** (monkeypatched sender) | `register_user` | Row created unverified; the sender is called once |

## D2. Test notes for Claude Code

- **Update existing seeds.** Every SPEC-01 test that seeds a verified password row meant to be *legitimate* (M3, and any `login_user` success assertions) must now also set `password_proven_at=datetime.now(timezone.utc)`. Otherwise the new rules correctly reclaim it or demand a reset, and the test fails for the wrong reason.
- **M14 is the A1 exploit test.** Write it first and confirm it **fails** on the current PR head: the row is linked, and `login_user` with the squatter's password succeeds. Record that failure in the PR description, the same way SPEC-01 did for M4.
- **Migration:** `tests/test_migration_revision_graph.py` must pass with `0016` as the single head. The SQL `UPDATE` isn't unit-tested against SQLite; it's reviewed in the PR and exercised in step 3 of the runbook.
- **Email:** monkeypatch `email_service.send_verification_email` and `send_password_reset_email` to record calls. Never hit the network.

## D3. Acceptance criteria (in addition to SPEC-01 §10)

- [ ] M14 fails on the current PR head (evidence in the PR) and passes after the change.
- [ ] All D1 rows have tests; the full `pytest` suite passes on a **fresh virtualenv** installed only from `backend/requirements.txt` (proves A3).
- [ ] `grep -n "password_proven_at = " backend/app` hits only: `register_user` (dev opt-in branch), `verify_email` (transition branch), and `reset_password`.
- [ ] `alembic upgrade head` on a copy of production data runs `0016` without error; `alembic downgrade -1` drops the column.
- [ ] A local dev registration with no email provider and no opt-in returns 503 with the hint, and creates no row.
- [ ] Runbook steps 1–6 completed and noted in the PR (audit query output summarised, not pasted with emails).

## D4. Files (additions to SPEC-01 §11)

| File | Change |
|---|---|
| `backend/app/db/models/user.py` | `password_proven_at` column |
| `backend/app/db/migrations/versions/0016_password_provenance.py` | New migration (§B2) |
| `backend/app/exceptions/custom_exceptions.py` | `PasswordResetRequiredError`, `EmailDeliveryUnavailableError` |
| `backend/app/services/email_service.py` | `delivery_configured`; verification email copy line |
| `backend/app/services/auth_service.py` | Rule 2 condition (§B3); `login_user` gate (§B4); `register_user` delivery check and dev-opt-in provenance (§B1.2, §B5.3); `verify_email` transition provenance; `reset_password` provenance; `forgot_password` delivery check |
| `backend/requirements.txt` | `aiosqlite` pin |
| `backend/tests/unit/test_google_account_linking.py` | Seed updates; M12–M23 |

## D5. Implementation order

1. Add `aiosqlite` to `requirements.txt`; recreate the venv from scratch; run the existing suite green (A3 first, so every later step runs on a reproducible base).
2. Write **M14** against the current PR head; confirm it fails as described. **Stop and report if it doesn't.**
3. Model column and migration `0016`; run `alembic upgrade head` locally.
4. `auth_service`: provenance writes (B1.2), then the Rule 2 condition (B3), then the login gate (B4). Update the SPEC-01 test seeds (D2) as you go.
5. Errors, `delivery_configured`, the register and forgot-password checks (B5). Email copy line.
6. Remaining D1 tests; full suite on a fresh venv.
7. PR description: the M14 before/after, the runbook checklist, and the A3 blast-radius note.

## D6. Out of scope (unchanged from SPEC-01 §12, plus)

- A frontend "Reset password" button keyed on `code = password_reset_required`. The message text is sufficient for the MVP.
- Automated quarantine of data owned by unproven rows (manual audit via §B7 for now).
- Splitting `requirements-dev.txt` out of `requirements.txt`.
- Password-reset token reuse (Issue #6 in the security review). The forced reset makes reset links more valuable, so prioritise this next.