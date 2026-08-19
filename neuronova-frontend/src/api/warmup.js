// Fire-and-forget warmup ping for the Render backend.
//
// Render's free tier spins down the backend after ~15 minutes of
// inactivity, so the first real user request can pay a ~50s cold start.
// This module sends one unauthenticated GET to the public /health endpoint
// on app load to reduce (not eliminate) the chance of that happening.
//
// Intentionally does NOT import `api` from `./client`: `fetchWithIntercept`
// can clear tokens and redirect to /login on a failed refresh, and a warmup
// ping must never be able to log a user out or navigate. This module sends
// no Authorization and no X-API-Key header, and swallows every outcome.

const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1'

let fired = false

export function warmBackend() {
  if (fired) return
  fired = true

  const url = `${BASE_URL.replace(/\/+$/, '')}/health`

  try {
    const resolved = new URL(url, window.location.href)
    if (resolved.origin === window.location.origin) {
      // The base is relative, so this request resolves against the current
      // origin instead of the Render backend. In local dev a Vite proxy may
      // forward it to the real backend, so we still fire it — but on Vercel
      // the SPA rewrite in vercel.json will answer with index.html (a 200),
      // and nothing gets warmed.
      console.warn(
        'warmBackend: VITE_API_BASE_URL is relative, so this warmup ping cannot reach the Render backend.'
      )
    }
  } catch {
    // Ignore URL resolution failures; still attempt the fetch below.
  }

  fetch(url, { cache: 'no-store', keepalive: true }).catch(() => {})
}
