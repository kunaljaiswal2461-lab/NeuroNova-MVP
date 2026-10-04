export const RETRY_MESSAGE = 'Backend is temporarily unavailable. Retrying...'
export const UNAVAILABLE_MESSAGE = 'Processing may still be running. Please try again shortly.'
export const UPLOAD_UNCERTAIN_MESSAGE = 'The upload may still be processing. Check its status before uploading again to avoid a duplicate.'
export const JWT_REQUIRED_MESSAGE = 'Please sign in again to manage database connections.'

const TRANSIENT_STATUSES = new Set([502, 503, 504])
const READ_TIMEOUT_MS = 15_000
const LONG_TIMEOUT_MS = 120_000

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

  async function* streamSSE(path, body = {}, options = {}) {
    // One deadline spans connection and body consumption. Streams are never
    // retried: reconnecting a chat mutation could generate a duplicate reply.
    // Do not replay a stream after a token refresh either: a server may have
    // accepted the turn before the browser receives a failed stream response.
    if (options.requireJwt && !storage.getItem('access_token')) throw jwtRequired()
    const scope = deadline(options.signal, options.timeoutMs ?? longTimeoutMs)
    let reader
    try {
      const res = await scope.run(() => fetchAuthenticated(path, {
        method: 'POST', headers: authHeaders(options), body: JSON.stringify(body),
      }, scope.signal, {}, { preserveErrorBody: options.preserveErrorBody }, false))
      reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await scope.run(() => reader.read())
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''
        for (const part of parts) {
          if (!part.trim()) continue
          const eventMatch = part.match(/^event:\s*(.+)$/m)
          const dataMatch = part.match(/^data:\s*(.+)$/ms)
          if (eventMatch && dataMatch) {
            let data
            try {
              data = JSON.parse(dataMatch[1])
            } catch {
              continue // Preserve skipping malformed frames.
            }
            yield { event: eventMatch[1].trim(), data }
          }
        }
      }
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

  return { get, getBlob, post, put, postForm, delete: del, streamSSE }
}
