# SPEC-01 — Google sign-in account-linking takeover

| Field | Value |
|---|---|
| Status | Proposed |
| Severity | **Critical** |
| Area | Authentication (`backend/app/services/auth_service.py`, `backend/app/api/routes/auth_routes.py`, `backend/app/core/config.py`) |
| Audience | Siddhant (owner, reviewer) and Claude Code (implementer) |
| Code baseline | `NeuroNova-MVP-main` snapshot uploaded on 2026-10-05. Line numbers below refer to that snapshot; re-verify them before editing. |
| Related, out of scope | SPEC-02 (JWT secret startup guard), the OAuth `state` CSRF issue, the token-storage issue |

---

## 0. How to use this spec

**For Siddhant:** Part A explains the bug from first principles: what each line does, what it silently assumes, and how an attacker breaks that assumption. Part B explains the fix the same way, including the alternatives that were rejected and why. Section 10 is the checklist you review against.

**For Claude Code:**

1. Read the whole spec before editing anything.
2. Work test-first, in the order given in §11. The first exploit test (`test_preregistered_password_does_not_survive_google_link`) **must fail on the current code** before any fix is applied. If it passes on the unfixed code, stop and report back: the analysis or the test is wrong.
3. Touch only the files listed in §11. Do not refactor unrelated auth code, rename functions, or change the token format.
4. Every behaviour in the matrix in §8 must have a test.

---

# PART A — The bug

## 1. The trust model this code is supposed to enforce

NeuroNova has two ways to prove you are the owner of an email address:

| Proof | Where it happens | What it proves |
|---|---|---|
| Email verification link | `register_user` sends it; `verify_email` consumes it | Whoever clicked the link can read that inbox |
| Google sign-in | `google_oauth_callback` → `get_or_create_google_user` | Google says this Google account controls that email, **but only if Google also says `email_verified: true`** |

There is one credential that does *not* prove anything about the email: **the password typed at registration**. Anyone can type any email into `/auth/register`. Until that email is verified, the password on the row belongs to *whoever typed it*. That is not necessarily the person who owns the inbox.

From this model, the invariant the system must keep is:

> **INV-0: A credential is only trusted for an account if it was set by someone who has proven ownership of that account's email.**

The bug is that Google sign-in breaks INV-0.

## 2. The vulnerable code, line by line

### 2.1 `register_user` creates a row with an unproven password (`auth_service.py` L32–77)

```python
35  existing = await db.scalar(select(User).where(User.email == email.lower()))
36  if existing:
37      raise ConflictError("email already registered")
...
43  dev_auto_verify = (
44      settings.app_env == "dev"
45      and not settings.smtp_host
46      and not settings.resend_api_key
47  )
...
49  user = User(
50      email=email.lower(),
51      name=name,
52      hashed_password=hash_password(password),
53      is_active=True,
54      is_verified=dev_auto_verify,
55      role=UserRole.USER,
56  )
```

| Line | What it does | What it assumes / why it matters |
|---|---|---|
| 35–37 | Rejects a second registration of the same email. | The **first** person to register an email claims the row. That can be an attacker. |
| 43–47 | Decides whether to skip email verification. | Fires when `app_env == "dev"` **and** no email provider is configured. `app_env` defaults to `"dev"` (`config.py` L78). A production deploy that forgets `APP_ENV=prod`, or has not configured email yet, auto-verifies every signup. See Attack C. |
| 51 | Stores the attacker-chosen display name. | Shown later in the victim's UI. |
| 52 | Stores the password hash. | **This is the unproven credential.** Whoever typed the password owns it. |
| 54 | Normally `False`. | This flag is the *only* thing that marks the password as untrusted. |

### 2.2 `login_user`, where the unproven password is held back (`auth_service.py` L80–101)

```python
83  user = await db.scalar(select(User).where(User.email == email.lower()))
84  if not user or not user.hashed_password or not verify_password(password, user.hashed_password):
85      raise UnauthorizedError("invalid credentials")
86  if not user.is_active:
87      raise UnauthorizedError("account disabled")
88  if not user.is_verified:
89      raise UnauthorizedError("email not verified — check your inbox")
```

| Line | What it does | Why it matters |
|---|---|---|
| 84 | Checks the password. | Passes for the attacker, because they chose it. |
| 88–89 | Blocks unverified accounts. | **This is the only gate** stopping the attacker. The password is already "correct"; the system is only waiting for `is_verified` to flip to `True`. Anything that flips that flag without also removing the password hands the attacker a working login. |

### 2.3 `google_oauth_callback` trusts Google's `email` claim blindly (`auth_routes.py` L280–345)

```python
322  userinfo_resp = await client.get(
323      "https://www.googleapis.com/oauth2/v3/userinfo", ...)
326  userinfo_resp.raise_for_status()
327  info = userinfo_resp.json()
...
330  access_token, refresh_token, _user = await auth_service.get_or_create_google_user(
331      db, settings,
332      google_id=info["sub"],
333      email=info["email"],
334      name=info.get("name", info["email"]),
335      avatar_url=info.get("picture"),
336  )
```

| Line | What it does | What's wrong |
|---|---|---|
| 327 | Parses the userinfo response, which includes `sub`, `email`, `email_verified`, `name`, `picture`. | `info["email_verified"]` is **never read**. Google's documentation says the `email` claim should only be trusted as proof of email ownership when `email_verified` is true. |
| 333 | Passes the email to the service as if it were proven. | Unverified emails are treated as proof of ownership. |
| 333–334 | `info["email"]` with no fallback. | If Google omits `email`, this raises `KeyError` and the request fails with a 500 instead of a clean rejection. That's a minor robustness issue, but it gets fixed in the same place. |

### 2.4 `get_or_create_google_user`, where INV-0 breaks (`auth_service.py` L191–235)

```python
199  # 1. Try by google_id
200  user = await db.scalar(select(User).where(User.google_id == google_id))
201  if not user:
202      # 2. Try by email (link existing account)
203      user = await db.scalar(select(User).where(User.email == email.lower()))
204      if user:
205          user.google_id = google_id
206          if avatar_url and not user.avatar_url:
207              user.avatar_url = avatar_url
208          if not user.is_verified:
209              user.is_verified = True  # Google confirms the email
210      else:
211          # 3. New user via Google
...
```

| Line | What it does | What it assumes | Is the assumption true? |
|---|---|---|---|
| 200 | Looks up a user by Google's stable subject id. | A previously linked Google account maps to its user. | ✅ Yes. Safe. |
| 203 | Falls back to looking up by email. | Same email means same person. | ❌ Only if *both* sides proved the email. Google's side is unchecked (§2.3), and the row may have an unproven password (§2.1). |
| 205 | Attaches the Google identity to the row. | The row belongs to the Google user. | ❌ The row may have been created by an attacker. |
| 206–207 | Keeps the existing avatar if set. | Minor. | — |
| **208–209** | **Marks the row verified.** | "Google confirms the email, so the row is now trustworthy." | ❌ **This is the bug.** Google confirms the *email*. It does not confirm the *password* already sitting in `hashed_password`. Flipping `is_verified` upgrades the trust of the **whole row**, including a password set by an unknown party. That is exactly the gate from §2.2 L88 opening for the attacker. |

**The bug in one sentence:** step 2 proves the email through Google and then trusts every credential on the row, including a password that was never proven, so the person who typed that password can log in.

## 3. Exploits, with worked examples

The worked examples use these names:

- Victim: Priya, `priya@acme-logistics.com`, who has a Google Workspace account for that address.
- Attacker: Mallory, who knows Priya's work email (it's on LinkedIn).

### Attack A: pre-account hijacking (realistic, needs no Google trickery)

| Step | Actor action | Effect | `users` row state |
|---|---|---|---|
| A1 | Mallory calls `POST /auth/register` with `{email: "priya@acme-logistics.com", name: "Priya", password: "Mall0ry!pass"}`. | Row created (§2.1 L49–56). A verification email goes to Priya's inbox. | `email=priya@…`, `hashed_password=H(Mall0ry!pass)`, `is_verified=false`, `google_id=null` |
| A2 | Priya ignores the email ("I never signed up for this"). | Nothing. | unchanged |
| A3 | Mallory tries `POST /auth/login`. | Password passes (L84), blocked at L88 "email not verified". | unchanged |
| A4 | Weeks later, Priya's team adopts NeuroNova. She clicks **Sign in with Google**. | Google returns `sub=G-PRIYA`, `email=priya@…`, `email_verified=true`. | — |
| A5 | `get_or_create_google_user` runs. L200 misses (`G-PRIYA` unknown). L203 finds Mallory's row. L205 links. **L209 sets verified.** | Priya is logged in and sees a normal, empty workspace. | `hashed_password=H(Mall0ry!pass)`, `is_verified=true`, `google_id=G-PRIYA` |
| A6 | Priya connects Acme's production PostgreSQL (`orders`, `customers`, `invoices`). | The source is stored with `owner_user_id = this row`. | unchanged |
| A7 | Mallory calls `POST /auth/login` with `Mall0ry!pass`. | L84 ✅, L86 ✅, **L88 ✅ (now verified)**. Mallory gets an access token and a refresh token for Priya's account. | unchanged |
| A8 | Mallory opens Database Chat on Priya's source. | Mallory can query Acme's data within the approved scope, read Priya's chat history (including typed result rows), see the catalog and metric definitions, retest the source, change its scope, and delete it. | — |

**What Priya sees:** nothing. There is no "new device" notice, no password-change email, and no second-user indicator. Mallory's login is an ordinary password login.

### Attack B: Google account with an unverified email (lower likelihood, same impact)

If a Google account presents `email=priya@acme-logistics.com` with `email_verified=false`:

| Step | Effect |
|---|---|
| B1 | Priya already has a normal, verified NeuroNova account. |
| B2 | Mallory signs in with Google. L200 misses, L203 finds Priya's real account, L205 links Mallory's `google_id` to it. |
| B3 | Mallory now has a permanent Google login into Priya's account. |

In practice, getting Google to emit an unverified email claim for an address you don't control is hard. But the check costs one line, and Google's guidance is explicit, so the server must not depend on it being hard.

### Attack C: the dev auto-verify amplifier (turns A into a no-wait attack)

If production runs with the default `app_env="dev"` and no Resend or SMTP configured (both are realistic during an MVP rollout):

| Step | Effect |
|---|---|
| C1 | Mallory registers `priya@acme-logistics.com`. L43–47 evaluates to `True`, so **the row is created verified**. |
| C2 | Mallory can log in **immediately**. No waiting is needed. |
| C3 | When Priya later uses Google sign-in, L203 finds the (verified) row and links it. Priya now lives inside Mallory's account. |

**Important consequence for the design:** in Attack C the row is *already verified* when Google links it. A fix that only handles unverified rows (Rule 2 below) **does not stop Attack C**. Attack C must be closed separately in config (Rule 4). That is why both rules are in this spec.

## 4. Why this is Critical

| Factor | Assessment |
|---|---|
| Attacker prerequisites | Knows the victim's email address. No credentials, no network position, no malware. |
| Attack cost | One HTTP request (A1), then waiting. |
| Victim interaction | Must sign in with Google once. That's the normal, encouraged sign-in path. |
| Blast radius | Not just a NeuroNova account. The account owns **live connections to customers' production databases** (credentials stored encrypted, usable through the app). Mallory reaches Acme's data through NeuroNova's own controlled query path. The read-only and scope guards limit *what* she can read, but they don't stop *who* reads it. |
| Detectability | Close to zero. Both parties use legitimate login paths. Logs show `auth.google` for Priya and `auth.login` for the same user ID from a different IP. Nothing alerts on that. |
| Persistence | Mallory's password stays valid until someone resets it. Refresh tokens last 7 days. |
| Trust impact | This is a B2B data tool. One incident where a customer's database was exposed through another person's login ends the product's credibility. |

Low effort, no privileges, and impact that crosses into a customer's production database together make this **Critical**. It must be fixed before any external user connects a real database.

---

# PART B — The MVP solution

## 5. Invariants the fix must establish

| ID | Invariant | Kills |
|---|---|---|
| INV-1 | A Google identity is used as proof of email ownership **only** when Google reports `email_verified` as the boolean `true`. | Attack B |
| INV-2 | When Google proves the email for a row that was **never** proven, every credential on that row that came from the unproven party is destroyed: the password, outstanding refresh tokens, and the attacker-chosen profile fields. | Attack A |
| INV-3 | A row already linked to Google identity X is never silently re-linked to identity Y. | Defense in depth for B and future providers |
| INV-4 | Email verification can only be skipped through an **explicit opt-in** flag, and only in `dev`. A forgotten env var can never turn it on. | Attack C |
| INV-5 | Legitimate linking still works: a **verified** password user who signs in with Google keeps their password and gets Google linked. | Regression guard |

## 6. The design, line by line

### 6.1 `get_or_create_google_user` (replacement)

```python
 1  async def get_or_create_google_user(
 2      db: AsyncSession,
 3      settings: Settings,
 4      google_id: str,
 5      email: str | None,
 6      email_verified: object,
 7      name: str,
 8      avatar_url: str | None,
 9  ) -> tuple[str, str, User]:
10      # Rule 1 (INV-1): Google's email is proof only when explicitly verified.
11      if email_verified is not True or not email:
12          log.warning("auth.google.rejected_unverified_email", google_id=google_id)
13          raise UnauthorizedError("Your Google account's email address is not verified.")
14      normalized_email = email.lower()
15
16      user = await db.scalar(select(User).where(User.google_id == google_id))
17      if user is None:
18          user = await db.scalar(select(User).where(User.email == normalized_email))
19          if user is not None:
20              # Rule 3 (INV-3): never silently move an account to another Google identity.
21              if user.google_id is not None and user.google_id != google_id:
22                  log.warning("auth.google.link_conflict", user_id=str(user.id))
23                  raise UnauthorizedError("Google sign-in could not be completed for this account.")
24              if not user.is_verified:
25                  # Rule 2 (INV-2): the row was never proven; its credentials are untrusted.
26                  await _reclaim_unproven_account(db, user, name=name, avatar_url=avatar_url)
27              elif avatar_url and not user.avatar_url:
28                  user.avatar_url = avatar_url
29              user.google_id = google_id
30          else:
31              user = User(
32                  email=normalized_email,
33                  name=name,
34                  hashed_password=None,
35                  is_active=True,
36                  is_verified=True,
37                  role=UserRole.USER,
38                  google_id=google_id,
39                  avatar_url=avatar_url,
40              )
41              db.add(user)
42              await db.flush()
43
44      if not user.is_active:
45          raise UnauthorizedError("account disabled")
46      # ... unchanged: issue access + refresh token, commit, log "auth.google"
```

| Line | Decision | Why this is the right choice |
|---|---|---|
| 6 | `email_verified` becomes a required parameter typed `object`, not `bool`. | The service is the single place where the trust decision is made. Typing it `object` documents that the raw claim is passed through **unconverted**, so the check on L11 sees exactly what Google sent. Making it required means any future caller (another OAuth provider, an admin tool) can't forget it, because the code won't run without it. |
| 11 | `email_verified is not True`, not `if not email_verified`. | A truthiness check accepts `"true"`, `"false"` (a non-empty string is truthy!), `1`, and `[0]`. The v3 userinfo endpoint returns a JSON boolean, so only the singleton `True` is accepted. Example: `if not "false"` is `False`, so a truthiness check would **accept** a string `"false"`. The identity check rejects it. |
| 11 | `or not email` | A missing or empty email is treated as unverified. This also removes the `KeyError` → 500 path from §2.3. |
| 12 | Warning log with `google_id` only. | Gives you an audit trail of rejected attempts without logging the unverified email (PII that hasn't been proven). |
| 13 | `UnauthorizedError` with a user-readable message. | The callback already catches `UnauthorizedError` and redirects to `/login?error=…` (`auth_routes.py` L337–340), so no new error plumbing is needed. |
| 14 | Normalize once. | The original code called `email.lower()` in two places. Normalizing once guarantees lookup and insert use the same value. |
| 16 | The Google-ID lookup is unchanged. | A row already linked to this Google identity was linked under the rules, so it's trusted. |
| 21 | Reject a re-link when the row already has a *different* `google_id`. | Today this is unreachable only by accident: L16 would have matched if the IDs were equal. Writing it out makes the invariant explicit and survives future refactors, such as adding a second provider. Without it, L29 would silently overwrite the existing link and lock the rightful owner out of their Google login. |
| 23 | Generic message. | Doesn't confirm to a prober which Google identity owns the account. |
| 24–26 | Unverified row → reclaim (see §6.2). | **This is the core fix for Attack A.** The row was never proven, so nothing on it is trusted. |
| 27–28 | Verified row → only fill in the avatar if it's missing. | INV-5. The owner proved this email through the verification link, so their password and profile are legitimate and stay. |
| 29 | Link after the trust decision. | Ordering matters. Linking happens only after Rule 3 passes and Rule 2 has cleaned the row. |
| 31–42 | New-user branch unchanged except it uses `normalized_email`. | The behaviour was already correct. |
| 44–45 | `is_active` check unchanged. | A disabled account stays disabled even through Google. |

### 6.2 `_reclaim_unproven_account` (new private helper in `auth_service.py`)

```python
 1  async def _reclaim_unproven_account(
 2      db: AsyncSession, user: User, *, name: str, avatar_url: str | None
 3  ) -> None:
 4      """Hand an unverified row to the party that just proved the email.
 5
 6      Everything on the row was supplied by whoever registered it, who never
 7      proved ownership of the email. Nothing from that party survives.
 8      """
 9      user.hashed_password = None
10      user.name = name
11      user.avatar_url = avatar_url
12      await db.execute(
13          update(RefreshToken)
14          .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
15          .values(revoked=True)
16      )
17      user.is_verified = True
18      log.warning("auth.google.reclaimed_unverified_account", user_id=str(user.id))
```

| Line | Decision | Why |
|---|---|---|
| 9 | Remove the password. | **This is the line that kills Attack A.** Re-run the worked example: at step A7, Mallory's login fails at L84 because `not user.hashed_password` is true. Priya loses nothing. She can add a password later through "forgot password", because `reset_password` sets a hash on any user (`auth_service.py` L174). |
| 10 | Overwrite the name with Google's. | The attacker chose the display name. Leaving it is a social-engineering vector. Example: Mallory registers with `name="Acme IT — verify at acme-help.io"`, and Priya sees that as her own profile name. |
| 11 | Overwrite the avatar unconditionally. | Same reasoning. For verified rows the original "only if missing" rule still applies (§6.1 L27). |
| 12–16 | Revoke all refresh tokens. | Defensive. An unverified row can't log in (§2.2 L88), so it *shouldn't* have tokens. But any future path that issues tokens before verification would silently reopen the hole. One `UPDATE` makes the invariant hold regardless. |
| 14 | `.is_(False)` | Produces `IS false` without the `== False` lint suppression used elsewhere, and works on both PostgreSQL and the SQLite test engine. |
| 17 | Mark verified last. | Trust is granted only after the untrusted state is removed. If anything above raises, the transaction (committed by the caller) never commits a "verified row that still has the attacker's password". |
| 18 | Warning-level log. | A reclaim means someone registered this email before its owner did. That's either an abandoned signup or a hijack attempt, and you want to see it in logs. |

**Leftover tokens, checked one by one.** After a reclaim, these old artifacts could still exist:

| Leftover | Who holds it | Is it a risk? |
|---|---|---|
| Email-verification JWT from A1 | Priya's inbox (it was sent to the row's email) | No. If clicked, `verify_email` sets `is_verified=True`, which it already is. |
| Password-reset JWT | Can only be requested through `forgot_password`, which emails `user.email`, which is Priya's inbox | No. Mallory never receives one. |
| Access JWT | Never issued, because the unverified row can't log in | No. |
| Refresh tokens | Revoked at L12–16 | No. |

### 6.3 `google_oauth_callback` (`auth_routes.py` L330–336), the call-site change

```python
access_token, refresh_token, _user = await auth_service.get_or_create_google_user(
    db, settings,
    google_id=info["sub"],
    email=info.get("email"),
    email_verified=info.get("email_verified"),
    name=info.get("name") or info.get("email") or "Google user",
    avatar_url=info.get("picture"),
)
```

| Change | Why |
|---|---|
| `email=info.get("email")` | A missing claim becomes `None` and is rejected cleanly by Rule 1 instead of raising a 500. |
| `email_verified=info.get("email_verified")` | Passes the raw claim through. The route makes **no** trust decision; the service is the single choke point. |
| `name=… or "Google user"` | `users.name` is `NOT NULL`. This guarantees a non-empty value even with a sparse profile. |
| `sub` stays `info["sub"]` | `sub` is always present in a valid userinfo response. A `KeyError` there means Google returned garbage, and failing loudly is correct. |

### 6.4 Rule 4, the explicit dev auto-verify opt-in (`config.py`, `auth_service.py`)

**`config.py`: add the field and a fail-closed validator.**

```python
# --- auth ---
# Local development only: skip email verification when no email provider is
# configured. Must be explicitly enabled; refused outside APP_ENV=dev.
auth_dev_auto_verify: bool = False

@model_validator(mode="after")
def _auth_dev_auto_verify_only_in_dev(self) -> "Settings":
    if self.auth_dev_auto_verify and self.app_env != "dev":
        raise ValueError("AUTH_DEV_AUTO_VERIFY is only allowed when APP_ENV=dev")
    return self
```

**`auth_service.register_user`: replace L43–47.**

```python
dev_auto_verify = (
    settings.auth_dev_auto_verify
    and settings.app_env == "dev"
    and not settings.smtp_host
    and not settings.resend_api_key
)
```

**`.env.example`: document it, commented out.**

```
# Local dev only. Skips email verification when no email provider is set.
# Refused at startup unless APP_ENV=dev. Never set this in staging/prod.
# AUTH_DEV_AUTO_VERIFY=true
```

| Decision | Why |
|---|---|
| A new flag defaulting to `False` | The current trigger is the **absence** of configuration (default env plus no email keys). A security bypass that switches on when something is *missing* fails open. With the flag, it switches on only when something is *present and deliberate*. |
| Keep the `app_env == "dev"` and no-email-provider conditions | Defense in depth. The flag alone isn't enough; it must also be dev with no real email delivery. |
| Validator raises when the flag is set outside dev | Turns a dangerous misconfiguration into a startup crash, which you notice in seconds, instead of silent auto-verification in production, which you might never notice. |
| Leave the `app_env` default at `"dev"` for now | Changing that default affects many other settings and is out of scope for this spec (see SPEC-02, which adds production startup guards). Rule 4 removes this bug's dependence on that default without touching it. |
| Developer impact | Local developers without email set `AUTH_DEV_AUTO_VERIFY=true` once in `.env`. That's a one-line, documented cost. |

**Operational check (one-time, not code):** search production logs for the event `auth.register.dev_auto_verified`. Any hits mean real signups were auto-verified in a deployed environment. Those accounts' passwords were never proven, so review them manually.

## 7. Why this design, and not the alternatives

| Alternative | Verdict | Reasoning |
|---|---|---|
| **Never auto-link by email.** A Google login with a matching email is refused, and users must link Google from Settings while logged in. | Rejected for MVP | It's the strictest option, but it needs new UI and an authenticated linking endpoint, and it breaks "Sign in with Google" for every existing password user. With INV-1 and INV-2 in place, linking by email is safe: it's only allowed when both sides have proven the email (verified row plus verified Google email), or when the row's unproven credentials are destroyed first. Revisit if you add enterprise SSO. |
| **Require the existing password before linking.** | Rejected | It doesn't help in Attack A: the "existing password" is Mallory's, and Priya doesn't know it. It only adds friction for legitimate verified users. |
| **Delete the unverified row and insert a fresh one.** | Rejected (equivalent security, more moving parts) | It achieves INV-2 too, but means a delete plus insert on a unique email in one transaction, a new user ID, and cascade behaviour to reason about. Reclaiming in place is a single-row update with the same security outcome, and an unverified row owns no data (sources, datasets, and chats all need a login). |
| **Treat any truthy `email_verified` as verified.** | Rejected | `"false"` is truthy in Python. An identity check against `True` is the only correct comparison for a security claim. |
| **Verify Google's ID token (JWKS) instead of calling userinfo.** | Not needed for MVP | Userinfo is fetched server-to-server over TLS, using an access token the server just got by exchanging the code with the client secret. The claims are equally authoritative here. ID-token verification adds key rotation and caching complexity for no security gain in this flow. |
| **Expire unverified registrations after 24h.** | Good follow-up, not a fix | It shrinks the window but doesn't close it: Mallory just re-registers. Rule 2 closes it at any age. |
| **Send an email when Google is linked to an existing account.** | Follow-up | It's useful detection for the owner (INV-5 path), but it needs a new template. It doesn't block merging this fix. |

## 8. Behaviour matrix (every row needs a test)

| # | Google claims | Existing state | Expected outcome |
|---|---|---|---|
| M1 | `email_verified=True` | Row with this `google_id` | Logged in, row unchanged |
| M2 | `email_verified=True` | No row for this email | New user created: verified, `hashed_password=None`, linked |
| M3 | `email_verified=True` | **Verified** password row, `google_id=None` | Linked; **password still works**; name kept; avatar filled only if empty |
| M4 | `email_verified=True` | **Unverified** password row | Reclaimed: `hashed_password=None`, name and avatar from Google, refresh tokens revoked, verified, linked; **old password login returns 401** |
| M5 | `email_verified=True` | Row with a **different** `google_id` | Rejected (`UnauthorizedError`), row unchanged |
| M6 | `email_verified=False` | Any | Rejected, no row created or modified |
| M7 | `email_verified="true"` (string) | Any | Rejected |
| M8 | `email_verified=None` (claim missing) | Any | Rejected |
| M9 | `email=None` (claim missing) | Any | Rejected, no 500 |
| M10 | `email_verified=True` | Inactive row | Rejected ("account disabled"), as today |
| M11 | Settings: `auth_dev_auto_verify=True`, `app_env="prod"` | — | `Settings(...)` raises at construction |
| M12 | Settings: flag `False`, `app_env="dev"`, no email provider | Register | Row created **unverified** |
| M13 | Settings: flag `True`, `app_env="dev"`, no email provider | Register | Row created verified (dev convenience preserved) |

## 9. Test plan

**Location:** `backend/tests/unit/test_google_account_linking.py` (new).

**Fixture pattern:** copy the in-memory SQLite approach from `tests/catalog/test_catalog_service.py`:
- `create_async_engine("sqlite+aiosqlite:///:memory:")`
- `create_all(tables=[User.__table__, RefreshToken.__table__])`
- Create users with an explicit `id=uuid.uuid4()`, because `uuid_generate_v4()` doesn't exist in SQLite.

**Seeding rows:** seed unverified and verified users **directly through the model** with `hashed_password=hash_password("…")`. Don't seed through `register_user`, because its dev auto-verify branch depends on settings and would make M4 setup ambiguous. For M12 and M13, call `register_user` and monkeypatch `email_service.send_verification_email` to a no-op async function.

**Settings:** build `Settings(...)` explicitly the same way existing unit tests do (`api_key` is required, minimum 8 characters).

**Proving the exploit (write first, run against unfixed code):**

```python
async def test_preregistered_password_does_not_survive_google_link(session, settings):
    # Attack A, steps A1–A7, compressed.
    attacker_row = User(id=uuid.uuid4(), email="priya@acme-logistics.com", name="Priya",
                        hashed_password=hash_password("Mall0ry!pass"),
                        is_active=True, is_verified=False, role=UserRole.USER)
    session.add(attacker_row); await session.commit()

    await auth_service.get_or_create_google_user(
        session, settings, google_id="G-PRIYA", email="priya@acme-logistics.com",
        email_verified=True, name="Priya Sharma", avatar_url=None)

    with pytest.raises(UnauthorizedError):
        await auth_service.login_user(session, settings,
                                      "priya@acme-logistics.com", "Mall0ry!pass")
```

On the **current** code this test fails twice over:
1. The call raises `TypeError` because `email_verified` isn't a parameter yet. Temporarily drop that argument to observe the real failure.
2. With the argument dropped, `login_user` **succeeds**: the exploit is real.

Record that failure in the PR description, then apply the fix.

**Remaining tests (one per matrix row):**

| Test | Matrix row |
|---|---|
| `test_known_google_id_logs_in_unchanged` | M1 |
| `test_new_google_user_created_verified_without_password` | M2 |
| `test_verified_password_user_links_and_keeps_password` | M3 (assert `login_user` with the original password still **succeeds**) |
| `test_preregistered_password_does_not_survive_google_link` | M4 (above), plus assert `name == "Priya Sharma"` and all refresh tokens revoked |
| `test_reclaim_revokes_refresh_tokens` | M4 (seed one unrevoked `RefreshToken` on the unverified row; assert it's revoked) |
| `test_link_conflict_rejected` | M5 (assert the row's `google_id` is unchanged) |
| `test_unverified_google_email_rejected` | M6 (assert no user row exists afterwards) |
| `test_string_true_email_verified_rejected` | M7 |
| `test_missing_email_verified_rejected` | M8 |
| `test_missing_email_rejected` | M9 |
| `test_inactive_user_rejected` | M10 |
| `test_dev_auto_verify_refused_outside_dev` | M11 |
| `test_register_unverified_without_opt_in` | M12 |
| `test_register_auto_verified_with_opt_in` | M13 |

## 10. Acceptance criteria

- [ ] All §9 tests pass. The full existing suite (`pytest` from `backend/`) still passes.
- [ ] The PR description includes the M4 test failing against the unfixed code.
- [ ] `get_or_create_google_user` requires `email_verified`, and the callback passes `info.get("email_verified")`.
- [ ] No code path anywhere sets `is_verified = True` on a row that still has a password supplied before verification. Verify with `grep -n "is_verified = True" backend/app`: every hit must be either `verify_email` (the owner clicked their own link) or `_reclaim_unproven_account` (the password was cleared first).
- [ ] `Settings(auth_dev_auto_verify=True, app_env="prod", …)` raises.
- [ ] `.env.example` documents `AUTH_DEV_AUTO_VERIFY` commented out.
- [ ] Production logs searched for `auth.register.dev_auto_verified` and the result noted in the PR.
- [ ] No changes outside the files listed in §11.

## 11. Implementation order for Claude Code

1. **Create** `backend/tests/unit/test_google_account_linking.py` with the fixture and the M4 exploit test. Run it and confirm the exploit (see §9). **Stop and report if it does not reproduce.**
2. **Edit** `backend/app/services/auth_service.py`:
   - import `update` from `sqlalchemy`;
   - add `_reclaim_unproven_account` (§6.2);
   - replace `get_or_create_google_user` (§6.1);
   - replace the `dev_auto_verify` expression in `register_user` (§6.4).
3. **Edit** `backend/app/api/routes/auth_routes.py`: the call site only (§6.3).
4. **Edit** `backend/app/core/config.py`: add `auth_dev_auto_verify` and the validator (§6.4). Import `model_validator` if it isn't already imported.
5. **Edit** `.env.example` at the repo root (§6.4).
6. **Add** the remaining tests (§9 table) and run the full suite.
7. **Report:** a diff summary, test output, and the M4 before/after evidence.

## 12. Out of scope (tracked separately)

- **SPEC-02:** production startup guards (JWT secret default, `app_env` default).
- **OAuth `state` not bound to the browser:** login CSRF, where a victim is logged into the attacker's account. A different bug with a different fix (nonce cookie).
- **Tokens in `localStorage` and non-rotating refresh tokens.**
- **Concurrent first-time Google callbacks for the same identity:** two simultaneous callbacks can both reach L41 and one hits the `google_id` unique constraint, giving a 500. It's not exploitable; handle it in a later hardening pass by catching `IntegrityError` and retrying the lookup.
- **Link-notification email and expiry of unverified registrations** (§7 follow-ups).
---

# PART C — Pull request content

## PR title

```
fix(auth): stop Google sign-in from handing pre-registered accounts to attackers (SPEC-01)
```

## PR description

```markdown
## Summary

Fixes a **critical account-takeover** in Google sign-in (SPEC-01). Anyone who knew a victim's email could register that address with their own password; when the victim later signed in with Google, the account was marked verified **without removing the attacker's password**, so the attacker could log in as the victim and reach any production database the victim connected.

## The issues

| # | Issue | Where | Impact |
|---|---|---|---|
| A | **Pre-account hijacking.** `get_or_create_google_user` linked Google to an existing row by email and set `is_verified = True`, but left the password set by the unverified registrant in place. | `auth_service.py` | Attacker logs in with their password to the victim's account, with access to the victim's connected databases, chats and sources. The victim sees nothing. |
| B | **Unverified Google email trusted.** `email_verified` from Google was never read. | `auth_routes.py`, `auth_service.py` | A Google identity with an unverified email could be linked to an existing account. |
| C | **Dev auto-verify fails open.** Email verification was skipped whenever `APP_ENV=dev` (the default) and no email provider was configured, so a deploy with missing config verified every signup instantly. | `auth_service.register_user` | Makes A a no-wait attack, and a row that is already verified is not protected by the fix for A. |
| D | Missing `email`/`name` claims raised `KeyError` and returned a 500. | `auth_routes.py` | Robustness only. |

## The fixes

1. **Reclaim unproven accounts (A).** New `_reclaim_unproven_account`: when Google proves the email for a row that was never verified, the password is cleared, name and avatar are overwritten with Google's, all refresh tokens are revoked, and only then is the row marked verified. The attacker's password stops working. The owner can set a password later via "forgot password".
2. **Require `email_verified is True` (B).** Checked in the service with an identity check (not truthiness, since `"false"` is truthy). A missing or empty email is rejected cleanly. The route passes the raw claims through and makes no trust decision.
3. **Refuse to re-link (defence in depth).** A row already linked to a different `google_id` is rejected with a generic message.
4. **Explicit dev opt-in (C).** New `AUTH_DEV_AUTO_VERIFY` setting, default `false`. Auto-verify now needs the flag **and** `APP_ENV=dev` **and** no email provider. `Settings` refuses to start if the flag is set outside `dev`.
5. **Unchanged behaviour:** a verified password user who signs in with Google still gets Google linked and keeps their password and profile.

## Files changed

- `backend/app/services/auth_service.py`: reclaim helper, new Google linking rules, `register_user` opt-in.
- `backend/app/api/routes/auth_routes.py`: callback call site only.
- `backend/app/core/config.py`: `auth_dev_auto_verify` and startup validator.
- `.env.example`: documents `AUTH_DEV_AUTO_VERIFY` (commented out).
- `backend/tests/unit/test_google_account_linking.py`: 14 new tests (one per behaviour-matrix row M1 to M13).

## Evidence

**Exploit reproduced on the unfixed code.** With the fix removed and the new `email_verified` argument dropped (the old code does not accept it), `test_preregistered_password_does_not_survive_google_link` fails:

```
Failed: DID NOT RAISE <class 'app.exceptions.custom_exceptions.UnauthorizedError'>
```

That is, after Google linked the row, `login_user` with the attacker's password **succeeded**.

**With the fix:** `14 passed` in `tests/unit/test_google_account_linking.py`.

**Full backend suite:** before (original code) 296 passed, 99 errors. After 310 passed, 99 errors, 0 failures. The 99 errors are identical in both runs, all in `tests/catalog/*` and `tests/sources/test_source_service.py`, and all fail at SQLite table creation (`near "(": syntax error` from PostgreSQL-only defaults) before reaching any code under test. They are not caused by this PR.

**Grep check:** `grep -n "is_verified = True" backend/app` returns only `verify_email` and `_reclaim_unproven_account`.

## Deployment notes

- Local developers without an email provider must now set `AUTH_DEV_AUTO_VERIFY=true` in `.env` to keep auto-verified signups.
- Existing unverified rows that later sign in with Google are reclaimed (password removed) automatically; a `auth.google.reclaimed_unverified_account` warning is logged.
- Tests need `aiosqlite` installed; it is not currently in `requirements.txt`.

## Reviewer checklist

- [ ] M4 exploit test reviewed (fails before, passes after)
- [ ] `get_or_create_google_user` requires `email_verified`; callback passes `info.get("email_verified")`
- [ ] `Settings(auth_dev_auto_verify=True, app_env="prod", ...)` raises
- [ ] `.env.example` documents the flag, commented out
- [ ] **Before merge or deploy: search production logs for `auth.register.dev_auto_verified`.** Any hit means real signups were auto-verified in a deployed environment; review those accounts manually. (Not yet done: result to be noted here.)

## Out of scope (tracked separately)

SPEC-02 production startup guards, OAuth `state` CSRF, token storage, concurrent first-time Google callbacks, link-notification email and expiry of unverified registrations.
```
