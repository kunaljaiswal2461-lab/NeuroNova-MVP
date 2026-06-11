# NeuroNova Frontend ↔ Backend Wiring Spec

Drop this into Antigravity as context. It describes every backend contract the frontend needs: base URL, auth flow, every endpoint's request/response shape, error envelope, env vars, and a recommended client architecture.

---

## 1. Base URL & environment

- **Dev backend**: `http://localhost:8000`
- **API prefix**: all auth/chat/dataset routes live under `/api/v1`
- **Static viz files**: served from `http://localhost:8000/viz/<filename>` (already absolute URLs in chart responses)
- **Frontend dev URL (CORS-allowed, backend assumes this)**: `http://localhost:5173`

Create `neuronova-frontend/.env`:
```
VITE_API_BASE_URL=http://localhost:8000
```

CORS on the backend is wide-open (`allow_origins=["*"]`), so no extra config needed in dev.

---

## 2. Authentication model

The backend supports **two parallel auth schemes** on data routes — pick JWT for the UI:

| Scheme         | Header                          | Used by        |
| -------------- | ------------------------------- | -------------- |
| **JWT (user)** | `Authorization: Bearer <token>` | UI (use this)  |
| Legacy API key | `X-API-Key: <key>`              | scripts / CI   |

Auth routes (`/api/v1/auth/me`, `PATCH /me`) require JWT specifically.
Dataset + chat routes accept **either**, so the frontend must send the JWT on every request.

### Token storage

- **`access_token`** — short-lived JWT (default 30 min). Store in memory or `localStorage`. Sent as `Authorization: Bearer …`.
- **`refresh_token`** — opaque random string, 7-day lifetime. Store in `localStorage` (or `httpOnly` cookie if you build that on the BE later; today it's returned in the JSON body).
- On 401 from any data endpoint, call `POST /api/v1/auth/refresh` with the refresh token. If that fails too → redirect to login and clear both.

### Lifecycle

```
register → email-verification link → /auth/verified page
                                        ↓
                                      login  → { access_token, refresh_token, user }
                                        ↓
                            ┌─── normal API calls (Bearer access_token) ──┐
                            │                                              │
                            └── on 401 → POST /auth/refresh → retry once ──┘
                                              ↓ (refresh fails)
                                          logout → clear tokens → /login
```

### Important constraint

`login` returns **401 "email not verified — check your inbox"** until the user clicks the verification link. The UI must surface this distinctly from "wrong password" (both are 401, differentiate by the error `message`).

---

## 3. Error envelope

Every error comes back as JSON. Status codes used:

| Code | Meaning                              | When                                       |
| ---- | ------------------------------------ | ------------------------------------------ |
| 400  | `ValidationFailure`                  | bad input (e.g. expired reset token)       |
| 401  | `UnauthorizedError`                  | missing/bad token, wrong creds, unverified |
| 403  | forbidden                            | role mismatch                              |
| 404  | `NotFoundError`                      | resource doesn't exist                     |
| 409  | `ConflictError`                      | email already registered                   |
| 422  | FastAPI validation                   | malformed body                             |
| 429  | rate-limit (slowapi `RateLimitExceeded`) | hit per-IP limit                       |

Body shape (consistent across `register_exception_handlers`):
```json
{ "detail": "human-readable message" }
```
422 from pydantic has the standard `{ "detail": [ { "loc": [...], "msg": "...", "type": "..." } ] }`.

**Rate limits to surface in UI** (per-IP):
- `register`: 5/min
- `login`: 10/min
- `refresh`: 20/min
- `forgot-password`: 3/min
- `reset-password`: 5/min

---

## 4. Auth endpoints (full contract)

All paths prefixed `/api/v1/auth`.

### `POST /register`  →  201
```json
// req
{ "email": "a@b.com", "name": "Jane", "password": "min 8 chars" }
// res
{ "message": "registration successful — please check your email to verify your account" }
```
Errors: 409 (email taken), 422 (bad shape), 429.

### `POST /login`  →  200
```json
// req
{ "email": "a@b.com", "password": "…" }
// res
{
  "access_token": "<jwt>",
  "refresh_token": "<opaque>",
  "token_type": "bearer",
  "user": {
    "id": "uuid",
    "email": "a@b.com",
    "name": "Jane",
    "role": "USER",            // "USER" | "ADMIN"
    "is_verified": true,
    "avatar_url": null
  }
}
```
Errors: 401 ("invalid credentials" | "account disabled" | "email not verified — check your inbox").

### `POST /logout`  →  204 (no body)
```json
// req
{ "refresh_token": "<opaque>" }
```
Always 204 even if token unknown. **Access token stays valid until it expires** — frontend must also drop it from storage.

### `POST /refresh`  →  200
```json
// req
{ "refresh_token": "<opaque>" }
// res
{ "access_token": "<new jwt>", "token_type": "bearer" }
```
Errors: 401 (revoked/expired refresh) — treat as "session ended, force re-login".

### `GET /verify-email?token=<jwt>`
Hit by the user clicking the email link. On success it **redirects** to `${frontend_url}/auth/verified` (so build a `/auth/verified` route that just shows a success message and a "Go to login" CTA). If `frontend_url` is empty, returns JSON instead — but in this project it defaults to `http://localhost:5173`, so plan for the redirect.

### `POST /forgot-password`  →  202
```json
// req
{ "email": "a@b.com" }
// res
{ "message": "if that email is registered, a reset link has been sent" }
```
Always 202 — never leaks whether the email is registered. UI should just show a "check your inbox" toast.

### `POST /reset-password`
```json
// req
{ "token": "<jwt from email>", "new_password": "min 8" }
// res
{ "message": "password reset successfully — please log in" }
```
Token comes from the URL query string of the link in the email — build a `/auth/reset-password?token=...` page that reads it and posts. Successful reset revokes **all** existing refresh tokens.

### `GET /me`  (Bearer required)
Returns the same `user` shape as login.

### `PATCH /me`  (Bearer required)
```json
// req
{ "name": "New Name" }   // only name is editable today
// res
<UserResponse>
```

### `GET /google`  →  `{ "url": "https://accounts.google.com/o/oauth2/v2/auth?…" }`
Returns `{ "error": "..." }` if Google OAuth isn't configured (no client_id). Frontend should hide the "Sign in with Google" button when `error` is present.

### `GET /google/callback?code=…`
**Don't call this from the SPA.** Google redirects the browser directly to this backend URL. After processing, the backend currently returns the `AuthTokenResponse` JSON — you may want to ask the backend team to redirect to `${frontend_url}/auth/google-callback#access_token=…&refresh_token=…` so the SPA can pick them up. **As-is, the Google flow won't land tokens in the SPA — flag this and either:**
  1. Change the backend callback to redirect with tokens in the URL fragment, or
  2. Implement a popup window flow that reads the JSON response.

Tell me which and I'll wire the backend side.

---

## 5. Dataset & chat endpoints (auth required)

All accept `Authorization: Bearer <jwt>`. Existing routes (already used by the current pages):

```
POST   /api/v1/datasets/upload                  multipart file → { dataset_id, ... }
GET    /api/v1/datasets                         list user datasets
GET    /api/v1/datasets/{id}                    dataset detail
GET    /api/v1/datasets/{id}/profile            profile JSON
GET    /api/v1/datasets/{id}/findings           findings JSON
GET    /api/v1/datasets/{id}/charts             { "charts": ["http://localhost:8000/viz/...png", ...] }
GET    /api/v1/datasets/{id}/...                (see dataset_routes.py for the full set)

POST   /api/v1/chat/sessions                    create chat session
GET    /api/v1/chat/sessions                    list sessions
GET    /api/v1/chat/sessions/{id}               session + messages
DELETE /api/v1/chat/sessions/{id}               delete session
GET    /api/v1/chat/sessions/{id}/messages      messages
POST   /api/v1/chat/sessions/{id}/messages      send message
```

> The current `src/api/client.js` sends `X-API-Key`. Switch it to `Authorization: Bearer ${accessToken}` and add a 401-refresh interceptor. Keep `X-API-Key` only as a fallback if a dev token isn't present.

---

## 6. Recommended client architecture

### Files to create / modify

```
src/
  api/
    client.js               ← axios/fetch wrapper with Bearer + refresh interceptor
    auth.js                 ← register, login, logout, refresh, me, forgot, reset, googleStart
  context/
    AuthContext.jsx         ← { user, accessToken, login(), logout(), refresh(), loading }
  pages/
    Login.jsx
    Register.jsx
    VerifyEmailLanding.jsx       ← route /auth/verified  (just a confirmation page)
    ForgotPassword.jsx           ← route /auth/forgot
    ResetPassword.jsx            ← route /auth/reset-password?token=…
    GoogleCallback.jsx           ← only if you build the fragment-redirect variant
  components/
    ProtectedRoute.jsx      ← wraps routes, redirects to /login if !user
    UserMenu.jsx            ← avatar dropdown with logout
  routes (router):
    /login, /register, /auth/verified, /auth/forgot, /auth/reset-password,
    /auth/google-callback, plus your existing app routes wrapped in <ProtectedRoute>
```

### `AuthContext` shape

```ts
{
  user: User | null,
  accessToken: string | null,
  loading: boolean,         // true on initial mount while restoring from localStorage
  login(email, password): Promise<void>,
  register(email, name, password): Promise<void>,
  logout(): Promise<void>,
  refresh(): Promise<string>,   // returns new access token, throws if refresh failed
  updateProfile(patch): Promise<User>,
}
```

### Client interceptor logic (pseudocode)

```js
// request
config.headers.Authorization = `Bearer ${accessToken}`

// response error
if (err.status === 401 && !config._retried && refreshToken) {
  config._retried = true
  try {
    const { access_token } = await POST('/api/v1/auth/refresh', { refresh_token })
    setAccessToken(access_token)
    config.headers.Authorization = `Bearer ${access_token}`
    return axios(config)
  } catch {
    clearTokens()
    location.assign('/login')
  }
}
```

Single in-flight refresh: queue concurrent 401s so they all wait on one refresh call.

### Bootstrapping on app load

1. Read `access_token` + `refresh_token` from `localStorage`.
2. If `access_token` present, call `GET /api/v1/auth/me`.
   - 200 → set user, app renders authenticated.
   - 401 → try refresh, then `me` again. On failure → unauthenticated state.
3. While this runs, render a spinner (`loading: true`).

---

## 7. Page-by-page UI checklist

| Page                   | Backend calls                                                  | Notes |
| ---------------------- | -------------------------------------------------------------- | ----- |
| `Login`                | `POST /auth/login`, `GET /auth/google`                         | Show distinct error for unverified email |
| `Register`             | `POST /auth/register`                                          | After success → show "check inbox" screen, do NOT auto-login |
| `VerifyEmailLanding`   | —                                                              | Just a static page at `/auth/verified` with a "Continue to login" button |
| `ForgotPassword`       | `POST /auth/forgot-password`                                   | Always show generic success message |
| `ResetPassword`        | `POST /auth/reset-password`                                    | Read `token` from query string; on success redirect to `/login` |
| `UserMenu` / `Profile` | `GET /auth/me`, `PATCH /auth/me`, `POST /auth/logout`          | Show avatar (`avatar_url`) + initials fallback |
| Existing pages         | unchanged, but now via Bearer token                            | Wrap all in `<ProtectedRoute>` |

---

## 8. Validation rules to enforce client-side (matches BE)

- `email`: must be a valid email (BE uses pydantic `EmailStr`).
- `password`: min 8, max 128 chars.
- `name`: min 1, max 256 chars.
- Refresh token: opaque string, treat as a secret — never log it.

---

## 9. Things to ask me / the backend team before building

1. **Google OAuth callback strategy** — see §4. Without a decision, the Google button can't actually log a user in.
2. **Refresh token storage** — fine as `localStorage` for MVP; later move to `httpOnly` cookie set by BE.
3. **Token rotation on refresh?** — currently `/auth/refresh` returns a new access token but **not** a new refresh token. The refresh token stays valid for its full 7 days. Decide if you want rotation later.
4. **Verify-email page contents** — backend redirects but doesn't pass any params. The page can't show the user's email — just a generic success.

---

## 10. Quick smoke test (curl)

```bash
# register
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","name":"A","password":"password123"}'

# (verify email via the dev SMTP log / mailhog, then)

# login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","password":"password123"}'

# me
curl http://localhost:8000/api/v1/auth/me \
  -H "Authorization: Bearer <access_token>"
```
