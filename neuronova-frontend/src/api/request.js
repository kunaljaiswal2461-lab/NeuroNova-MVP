export const RETRY_MESSAGE = 'Backend is temporarily unavailable. Retrying...'
export const UNAVAILABLE_MESSAGE = 'Processing may still be running. Please try again shortly.'
export const UPLOAD_UNCERTAIN_MESSAGE = 'The upload may still be processing. Check its status before uploading again to avoid a duplicate.'
export const JWT_REQUIRED_MESSAGE = 'Please sign in again to manage database connections.'

const TRANSIENT_STATUSES = new Set([502, 503, 504])
const READ_TIMEOUT_MS = 15_000
const LONG_TIMEOUT_MS = 120_000
// SPEC-10 W1.3: a stream has three limits instead of one wall clock. Connect covers a Render cold start (~50 s); idle
// is four missed 10 s heartbeats; the ceiling sits above the server's 150 s turn budget plus a cold start.
const CONNECT_TIMEOUT_MS = 75_000
const IDLE_TIMEOUT_MS = 45_000
const MAX_STREAM_MS = 300_000

function cancellation() {
  return new DOMException('Request cancelled.', 'AbortError')
}

function unavailable(uncertain = false) {
  return Object.assign(new Error(uncertain ? UPLOAD_UNCERTAIN_MESSAGE : UNAVAILABLE_MESSAGE), { transient: true })
}

function friendlyError(error, uncertain = false) {
  if (error.name === 'AbortError') return error
  if (error.transient || error.name === 'TimeoutError' || error.name === 'NetworkError' || error instanceof TypeError) {
    return Object.assign(unavailable(uncertain), {
      retryable: error.retryable,
      status: error.status,
      response: error.response,
      code: error.code,
      ...(error.name === 'TimeoutError' ? { name: 'TimeoutError' } : {}),
    })
  }
  return error
}

// Race even a body reader (or a fetch mock) that does not reject on abort.
function abortable(task, signal) {
  if (signal.aborted) return Promise.reject(signal.reason)
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(signal.reason)
    signal.addEventListener('abort', onAbort, { once: true })
    Promise.resolve().then(() => {
      if (signal.aborted) throw signal.reason
      return task()
    }).then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort))
  })
}

function deadline(signal, timeoutMs) {
  const controller = new AbortController()
  const onAbort = () => controller.abort(cancellation())
  if (signal?.aborted) onAbort()
  else signal?.addEventListener('abort', onAbort, { once: true })
  const timer = setTimeout(() => {
    controller.abort(new DOMException(UNAVAILABLE_MESSAGE, 'TimeoutError'))
  }, timeoutMs)
  return {
    signal: controller.signal,
    run: task => abortable(task, controller.signal),
    close() {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      // Stop any unread/unfinished response as well as releasing listeners.
      controller.abort(cancellation())
    },
  }
}

function streamTimeout(code) {
  const messages = {
    connect_timeout: 'The server did not respond in time.',
    stream_idle_timeout: 'The connection went quiet while answering.',
    stream_max_timeout: 'The answer took longer than the maximum allowed time.',
  }
  return Object.assign(new Error(messages[code] ?? UNAVAILABLE_MESSAGE), { name: 'TimeoutError', code })
}

// Connect, idle and ceiling limits for one stream. `connected()` ends the connect phase and starts the idle timer;
// `touch()` (any bytes, a heartbeat comment included) restarts it.
function streamScope(signal, { connectTimeoutMs, idleTimeoutMs, maxStreamMs }) {
  const controller = new AbortController()
  const onAbort = () => controller.abort(cancellation())
  if (signal?.aborted) onAbort()
  else signal?.addEventListener('abort', onAbort, { once: true })
  const fail = code => controller.abort(streamTimeout(code))
  let connectTimer = setTimeout(() => fail('connect_timeout'), connectTimeoutMs)
  let idleTimer = null
  const maxTimer = setTimeout(() => fail('stream_max_timeout'), maxStreamMs)
  return {
    signal: controller.signal,
    run: task => abortable(task, controller.signal),
    connected() {
      clearTimeout(connectTimer)
      connectTimer = null
      idleTimer = setTimeout(() => fail('stream_idle_timeout'), idleTimeoutMs)
    },
    touch() {
      if (idleTimer === null) return
      clearTimeout(idleTimer)
      idleTimer = setTimeout(() => fail('stream_idle_timeout'), idleTimeoutMs)
    },
    close() {
      clearTimeout(connectTimer)
      clearTimeout(idleTimer)
      clearTimeout(maxTimer)
      signal?.removeEventListener('abort', onAbort)
      controller.abort(cancellation())
    },
  }
}

// One SSE frame: `event:` plus every `data:` line (joined with a newline, per the SSE rule). Comment lines are ignored.
function parseFrame(part) {
  let event = null
  const data = []
  for (const line of part.split('\n')) {
    if (!line || line[0] === ':') continue
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value[0] === ' ') value = value.slice(1)
    if (field === 'event') event = value.trim()
    else if (field === 'data') data.push(value)
  }
  return event && data.length ? { event, data: data.join('\n') } : null
}

function tokenExpiry(token) {
  try {
    const payload = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
    const exp = JSON.parse(atob(payload.padEnd(Math.ceil(payload.length / 4) * 4, '='))).exp
    return typeof exp === 'number' ? exp : null
  } catch {
    return null
  }
}

function backoff(ms, signal) {
  if (signal?.aborted) return Promise.reject(cancellation())
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      reject(cancellation())
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

async function validateResponse(res, { preserveErrorBody = false } = {}) {
  if (res.ok) return res
  if (TRANSIENT_STATUSES.has(res.status) && !preserveErrorBody) {
    throw Object.assign(unavailable(), { status: res.status })
  }
  let response
  let message = `Request failed (${res.status}). Please try again.`
  if (res.headers.get('content-type')?.includes('application/json')) {
    response = await res.json()
    const detail = response.error?.message || response.detail || response.message
    if (typeof detail === 'string') message = detail
    else if (Array.isArray(detail)) message = detail.map(item => item.msg || 'Invalid input').join('; ')
  }
  if (TRANSIENT_STATUSES.has(res.status)) {
    const code = response?.error?.code
    // Source onboarding needs to distinguish deployment and connection errors
    // returned in the normal API envelope. Other callers retain the historic
    // transient/retry behaviour without parsing or exposing those details.
    throw Object.assign(unavailable(), {
      status: res.status,
      ...(preserveErrorBody && response ? { response, code, retryable: false } : {}),
    })
  }
  if (/failed to fetch|networkerror|load failed/i.test(message)) message = UNAVAILABLE_MESSAGE
  throw Object.assign(new Error(message), { status: res.status, response })
}

// Browser-independent factory keeps the transport testable with node:test.
export function createApi({
  baseUrl,
  fallbackApiKey,
  storage,
  onAuthExpired = () => {},
  fetchImpl = (...args) => fetch(...args),
  readTimeoutMs = READ_TIMEOUT_MS,
  longTimeoutMs = LONG_TIMEOUT_MS,
  connectTimeoutMs = CONNECT_TIMEOUT_MS,
  idleTimeoutMs = IDLE_TIMEOUT_MS,
  maxStreamMs = MAX_STREAM_MS,
  retryDelays = [500, 1000],
}) {
  let refreshPromise = null

  function authHeaders(options = {}) {
    const token = storage.getItem('access_token')
    return {
      ...(token ? { Authorization: `Bearer ${token}` }
        : options.requireJwt ? {}
          : { 'X-API-Key': fallbackApiKey }),
      'Content-Type': 'application/json',
    }
  }

  function jwtRequired() {
    return Object.assign(new Error(JWT_REQUIRED_MESSAGE), {
      status: 401,
      code: 'authentication_required',
      retryable: false,
    })
  }

  function refresh() {
    if (refreshPromise) return refreshPromise
    const refreshToken = storage.getItem('refresh_token')
    const scope = deadline(undefined, readTimeoutMs)
    refreshPromise = scope.run(async () => {
      const res = await fetchImpl(`${baseUrl}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
        signal: scope.signal,
      })
      await validateResponse(res)
      const data = await res.json()
      if (scope.signal.aborted) throw scope.signal.reason
      if (!data.access_token) throw new Error('Unable to refresh your session. Please try again.')
      storage.setItem('access_token', data.access_token)
      return data.access_token
    }).catch(error => {
      // An outage is not evidence of an invalid session. Only explicit auth
      // rejection clears credentials; no failed refresh POST is replayed.
      if (error.status === 401 || error.status === 403) {
        storage.removeItem('access_token')
        storage.removeItem('refresh_token')
        onAuthExpired()
      }
      throw Object.assign(friendlyError(error), { retryable: false })
    }).finally(() => {
      scope.close()
      refreshPromise = null
    })
    return refreshPromise
  }

  // SPEC-10 W1.5. A hint, not a verification: refresh when the access token is about to expire (or cannot be read) so
  // the first question after a long idle does not meet a 401 that a stream may never retry. A transient refresh failure
  // never blocks the send; an explicit 401/403 goes through the normal `onAuthExpired` path inside `refresh()`.
  async function ensureFreshToken({ minValiditySeconds = 60 } = {}) {
    const token = storage.getItem('access_token')
    if (!token || !storage.getItem('refresh_token')) return
    const exp = tokenExpiry(token)
    if (exp !== null && exp - Date.now() / 1000 >= minValiditySeconds) return
    try {
      await refresh()
    } catch (error) {
      if (error.status === 401 || error.status === 403) throw error
    }
  }

  async function fetchAuthenticated(path, init, signal, state = {}, responseOptions = {}, allowAuthRefresh = true) {
    // A prior read attempt or another caller may already have refreshed auth.
    const currentToken = storage.getItem('access_token')
    if (currentToken) init = { ...init, headers: { ...init.headers, Authorization: `Bearer ${currentToken}` } }
    let res = await fetchImpl(`${baseUrl}${path}`, { ...init, signal })
    if (allowAuthRefresh && res.status === 401 && storage.getItem('refresh_token')) {
      // Refresh is shared, but each waiting caller can cancel independently.
      await res.body?.cancel()
      state.waitingForRefresh = true
      const token = await abortable(refresh, signal)
      if (signal.aborted) throw signal.reason
      state.waitingForRefresh = false
      // Explicit 401 means the operation was rejected, so the existing auth
      // handshake can resubmit it once. Never replay an uncertain write.
      res = await fetchImpl(`${baseUrl}${path}`, {
        ...init,
        headers: { ...init.headers, Authorization: `Bearer ${token}` },
        signal,
      })
    }
    return validateResponse(res, responseOptions)
  }

  async function request(path, init, consume, options = {}, uncertain = false) {
    // A source endpoint must never silently become a legacy API-key request.
    // Reject before fetch instead, while ordinary existing endpoints retain
    // their established fallback behaviour.
    if (options.requireJwt && !storage.getItem('access_token')) throw jwtRequired()
    const safeRead = !init.method || init.method === 'GET'
    for (let attempt = 0; attempt < (safeRead ? 3 : 1); attempt++) {
      const scope = deadline(options.signal, options.timeoutMs ?? (uncertain ? longTimeoutMs : readTimeoutMs))
      const state = {}
      try {
        return await scope.run(async () => {
          const res = await fetchAuthenticated(path, init, scope.signal, state, {
            preserveErrorBody: options.preserveErrorBody,
          })
          return consume(res)
        })
      } catch (error) {
        if (options.signal?.aborted) throw cancellation()
        const failure = friendlyError(error, uncertain)
        if (!safeRead || state.waitingForRefresh || !failure.transient || failure.retryable === false || attempt === 2) throw failure
      } finally {
        scope.close()
      }
      options.onRetry?.(RETRY_MESSAGE)
      await backoff(Math.min(retryDelays[attempt] ?? 1000, 2000), options.signal)
    }
  }

  function get(path, options = {}) {
    return request(path, { headers: authHeaders(options) }, res => res.json(), options)
  }

  function getBlob(path, options = {}) {
    return request(path, { headers: authHeaders(options) }, res => res.blob(), options)
  }

  function post(path, body = {}, options = {}) {
    return request(path, {
      method: 'POST', headers: authHeaders(options), body: JSON.stringify(body),
    // A cancellation acknowledgement intentionally has a 202 response with
    // no JSON body. Keep ordinary POSTs unchanged while allowing that narrow
    // acknowledged-empty contract to avoid parsing an empty response.
    }, res => options.expectEmptyResponse ? null : res.json(), options)
  }

  // Source-owned configuration is intentionally a replacement operation, not
  // an implicitly retryable create. Keep it on the same authenticated JSON
  // transport as POST while preserving the no-replay rule for writes.
  function put(path, body = {}, options = {}) {
    return request(path, {
      method: 'PUT', headers: authHeaders(options), body: JSON.stringify(body),
    }, res => options.expectEmptyResponse ? null : res.json(), options)
  }

  function postForm(path, formData, options = {}) {
    const headers = authHeaders(options)
    delete headers['Content-Type']
    return request(path, { method: 'POST', headers, body: formData }, res => res.json(), options, true)
  }

  function del(path, body, options = {}) {
    return request(path, {
      method: 'DELETE',
      headers: authHeaders(options),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }, res => res.status === 204 ? null : res.json(), options)
  }

  // Streams are never retried (reconnecting a chat mutation could generate a duplicate reply) and never replayed after
  // a token refresh (the server may have accepted the turn before the browser saw a failed response). The returned
  // iterator also exposes `skippedFrames`: frames whose JSON could not be parsed.
  function streamSSE(path, body = {}, options = {}) {
    const stats = { skippedFrames: 0 }
    const iterator = readStream(path, body, options, stats)
    Object.defineProperty(iterator, 'skippedFrames', { get: () => stats.skippedFrames })
    return iterator
  }

  async function* readStream(path, body, options, stats) {
    if (options.requireJwt && !storage.getItem('access_token')) throw jwtRequired()
    const scope = streamScope(options.signal, {
      connectTimeoutMs: options.connectTimeoutMs ?? connectTimeoutMs,
      idleTimeoutMs: options.idleTimeoutMs ?? idleTimeoutMs,
      maxStreamMs: options.maxStreamMs ?? options.timeoutMs ?? maxStreamMs,
    })
    let reader
    try {
      const res = await scope.run(() => fetchAuthenticated(path, {
        method: 'POST', headers: authHeaders(options), body: JSON.stringify(body),
      }, scope.signal, {}, { preserveErrorBody: options.preserveErrorBody }, false))
      scope.connected()
      reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let heldCarriageReturn = ''
      const frames = function* (text) {
        for (const part of text.split('\n\n')) {
          if (!part.trim()) continue
          const frame = parseFrame(part)
          if (!frame) continue
          let data
          try {
            data = JSON.parse(frame.data)
          } catch {
            stats.skippedFrames++
            continue // Preserve skipping malformed frames.
          }
          yield { event: frame.event, data }
        }
      }
      while (true) {
        const { done, value } = await scope.run(() => reader.read())
        if (done) break
        scope.touch()
        // CRLF and bare CR are line breaks too; a CR at the end of a chunk may be half of a CRLF, so wait for more.
        let text = heldCarriageReturn + decoder.decode(value, { stream: true })
        heldCarriageReturn = text.endsWith('\r') ? '\r' : ''
        if (heldCarriageReturn) text = text.slice(0, -1)
        buffer += text.replace(/\r\n?/g, '\n')
        const boundary = buffer.lastIndexOf('\n\n')
        if (boundary < 0) continue
        const complete = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        yield* frames(complete)
      }
      // The stream ended: flush the decoder and parse whatever is left as a last frame (no trailing blank line needed).
      buffer += (heldCarriageReturn + decoder.decode()).replace(/\r\n?/g, '\n')
      yield* frames(buffer)
    } catch (error) {
      if (options.signal?.aborted) throw cancellation()
      throw friendlyError(error)
    } finally {
      scope.close()
      if (reader) {
        // Do not let an unresponsive stream delay cancellation/route changes.
        reader.cancel().catch(() => {})
        reader.releaseLock()
      }
    }
  }

  return { get, getBlob, post, put, postForm, delete: del, streamSSE, ensureFreshToken }
}
