# Keep the Render backend warm and wake

## 1. Why this exists

Render's free-tier web services spin down after ~15 minutes with no inbound
traffic. The next request can pay a cold start of roughly a minute while
the instance boots back up. Successful external HTTP checks every 5 minutes
reduce idle spin-downs, but do not prevent restarts or all cold starts.

## 2. The endpoint

Both `/health` and `/api/v1/health` support **GET and HEAD** without a redirect.
GET returns HTTP 200 with the existing JSON payload:

```json
{ "success": true, "status": "ok", "timestamp": "2026-08-19T12:00:00Z" }
```

HEAD returns HTTP 200 with no response body. A `Content-Length` header may
describe the corresponding GET representation; it need not be zero for HEAD.
Unsupported methods such as POST still return 405.

- Public by design: no authentication, no API key required.
- Does no database work.
- Exempt from rate limiting, so a monitor (or a retry burst) can never cause
  a 429 on this path.
- `/api/v1/health` is also the path the Docker `HEALTHCHECK` in `Dockerfile`
  probes.

## 3. UptimeRobot setup

1. Log in to UptimeRobot → **Monitoring** → **+ New**.
2. Monitor type: **HTTP / website monitoring**.
3. URL to monitor: `https://<RENDER_SERVICE_NAME>.onrender.com/health`.
4. Notifications: select the email address that should receive alerts.
5. **Monitor interval**: drag the slider to **5 minutes** (1-minute checks
   require a paid plan).
6. **Location**: leave at `Default (auto-select)`.
7. Click **Create monitor**.

The default **HEAD** method is supported; changing to GET is not required.
Consider a **60-second request timeout** instead of 30 seconds to reduce false
alarms during startup. Some cold starts can still exceed that timeout.

## 4. `<RENDER_SERVICE_NAME>` is a placeholder

The Render production hostname is not stored anywhere in this repo (there is
no `render.yaml` or `Procfile`). Copy the real hostname from the Render
dashboard's service page before using the URL above — do not use
`<RENDER_SERVICE_NAME>` literally.

## 5. Frontend deploy invariant (required)

Both the Vercel **production** and **preview** environments must set:

```
VITE_API_BASE_URL=https://<RENDER_SERVICE_NAME>.onrender.com/api/v1
```

This is a requirement, not a suggestion. `vercel.json` only rewrites
unmatched paths to `index.html` — it does not proxy anything to Render. There
are two distinct failure modes if you get this wrong, and neither is obvious
from a status code:

- **Relative value** (e.g. `/api/v1`): every API call, including the warmup
  ping, resolves against the Vercel origin. The SPA rewrite answers with
  `index.html` and a **200 OK**, so nothing is warmed and nothing looks
  broken. Check the response body and host, not just the status.
- **Unset**: the frontend falls back to `http://localhost:8000/api/v1`, which
  points at the *visitor's* own machine. The request is typically blocked as
  mixed content on an HTTPS page, and never reaches Render either.

See `neuronova-frontend/.env.example` for the full variable list. Note that
`neuronova-frontend/.env.local` is gitignored and only affects local dev, not
Vercel deployments.

## 6. UptimeRobot free-tier limits

The free plan allows up to 50 monitors, with a minimum check interval of 5
minutes — enough for this use case without upgrading.

The configured interval applies while the monitor is up. During prolonged
downtime, UptimeRobot spaces checks farther apart, so a monitor that has been
down for a long time is not guaranteed to check every 5 minutes. See
[UptimeRobot's monitoring-interval explanation](https://help.uptimerobot.com/en/articles/11360876-what-is-a-monitoring-interval-in-uptimerobot).

## 7. Caveats

- **Instance-hours.** Keeping a free Render instance permanently awake burns
  through the account's monthly free instance-hours allowance faster than
  letting it sleep. If the account runs several free services, this can
  exhaust the quota sooner than expected.
- **Extra log volume.** A 5-minute monitor adds roughly 288 requests/day, and
  `TimingMiddleware` logs one `http.request` line per request, so this is
  about 288 extra log lines/day. That's negligible next to normal traffic and
  needs no code change today. If it ever becomes noisy, the minimal optional
  fix is a single early-return in `TimingMiddleware.dispatch` that skips
  logging when `request.url.path` is a health path — deliberately deferred,
  not implemented here.
- **Reduces, doesn't eliminate, cold starts.** A user who arrives during an
  actual spin-down (e.g. right after a deploy, or if the monitor is briefly
  down) still pays the full cold start.

## 8. Frontend warmup note

`neuronova-frontend/src/api/warmup.js` fires one unauthenticated `GET
.../health` request on app load, from `App.jsx`. It sends no `Authorization`
and no `X-API-Key` header, and swallows every outcome (network error, non-2xx
status, timeout) so it can never affect the UI or the user's session. If
`VITE_API_BASE_URL` is relative, it logs a console warning explaining that the
ping cannot reach the Render backend, but still fires the request (useful in
local dev with a proxy).

## 9. Recovering a monitor stuck Down with HTTP 405

Previously, these health routes only accepted GET. UptimeRobot's default HEAD
request therefore received **405 Method Not Allowed** even when the application
was running. The resulting prolonged Down status can space checks far enough
apart for Render to become idle. A response-time graph alone does not prove
successful checks: error responses also have a response time.

After deploying the GET/HEAD fix:

1. Allow the backend to finish starting. Verify **GET and HEAD return 200** on
   both `/health` and `/api/v1/health` at the actual Render origin. Confirm GET
   still returns the health JSON and HEAD has no response body. Do not mark
   405 as an acceptable status to hide the problem.
2. Pause and resume the existing UptimeRobot monitor once. This triggers a
   fresh check instead of waiting for the extended-downtime schedule. Keep its
   HEAD method and 5-minute interval; consider a 60-second timeout.
3. Verify the monitor becomes **Up** and observe several successful checks
   approximately 5 minutes apart. Correlate them with HEAD requests returning
   200 in Render's application logs.
4. Observe more than 15 minutes of continued monitor traffic and check for
   unexpected shutdowns. Continued idle shutdowns require checking the actual
   request gaps and monitor availability, not just its configured interval.

These are post-deployment operator steps; changing this repository does not
update the running service or UptimeRobot settings. Monitoring cannot guarantee
availability during deployments, restarts, or provider outages. An always-running
compute instance addresses idle sleep more reliably. The public homepage should
also render independently of session restoration so backend unavailability
cannot leave it blank; that frontend change is separate from this health fix.
