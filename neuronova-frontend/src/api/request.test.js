import test from 'node:test'
import assert from 'node:assert/strict'
import { createApi, RETRY_MESSAGE, UNAVAILABLE_MESSAGE, UPLOAD_UNCERTAIN_MESSAGE } from './request.js'

function json(data = {}, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function harness(fetchImpl, config = {}) {
  const tokens = new Map([['access_token', 'old'], ['refresh_token', 'refresh']])
  let logouts = 0
  const api = createApi({
    baseUrl: 'https://api.example.test',
    fallbackApiKey: 'test-key',
    storage: {
      getItem: key => tokens.get(key),
      setItem: (key, value) => tokens.set(key, value),
      removeItem: key => tokens.delete(key),
    },
    onAuthExpired: () => { logouts++ },
    retryDelays: [1, 1],
    fetchImpl,
    ...config,
  })
  return { api, tokens, get logouts() { return logouts } }
}

for (const status of [502, 503, 504]) {
  test(`GET retries ${status} and returns the successful parsed body`, async () => {
    let calls = 0
    const notices = []
    const { api } = harness(async () => ++calls < 3 ? json({}, status) : json({ done: true }))
    assert.deepEqual(await api.get('/read', { onRetry: message => notices.push(message) }), { done: true })
    assert.equal(calls, 3)
    assert.deepEqual(notices, [RETRY_MESSAGE, RETRY_MESSAGE])
  })
}

test('GET network failure exhausts exactly three attempts with friendly error', async () => {
  let calls = 0
  const { api } = harness(async () => { calls++; throw new TypeError('Failed to fetch') })
  await assert.rejects(api.get('/read'), { message: UNAVAILABLE_MESSAGE })
  assert.equal(calls, 3)
})

test('browser NetworkError is retried without exposing its native message', async () => {
  let calls = 0
  const { api } = harness(async () => { calls++; throw new DOMException('Network request failed', 'NetworkError') })
  await assert.rejects(api.get('/read'), { message: UNAVAILABLE_MESSAGE })
  assert.equal(calls, 3)
})

test('GET timeout aborts each attempt, including stalled body consumption', async () => {
  let calls = 0
  let aborted = 0
  const { api } = harness(async (_url, { signal }) => {
    calls++
    signal.addEventListener('abort', () => { aborted++ }, { once: true })
    return { ok: true, status: 200, json: () => new Promise(() => {}) }
  }, { readTimeoutMs: 5 })
  await assert.rejects(api.get('/read'), { message: UNAVAILABLE_MESSAGE })
  assert.equal(calls, 3)
  assert.equal(aborted, 3)
})

test('GET timeout bounds a stalled connection', async () => {
  let calls = 0
  const { api } = harness(() => { calls++; return new Promise(() => {}) }, { readTimeoutMs: 5 })
  await assert.rejects(api.get('/read'), { message: UNAVAILABLE_MESSAGE })
  assert.equal(calls, 3)
})

test('cancellation before fetch makes no request', async () => {
  let calls = 0
  const { api } = harness(async () => { calls++; return json() })
  const controller = new AbortController()
  controller.abort('route changed')
  await assert.rejects(api.get('/read', { signal: controller.signal }), { name: 'AbortError', message: 'Request cancelled.' })
  assert.equal(calls, 0)
})

test('cancellation during fetch is distinct and does not retry', async () => {
  let calls = 0
  const started = deferred()
  const { api } = harness(() => { calls++; started.resolve(); return new Promise(() => {}) })
  const controller = new AbortController()
  const result = api.get('/read', { signal: controller.signal })
  await started.promise
  controller.abort()
  await assert.rejects(result, { name: 'AbortError' })
  assert.equal(calls, 1)
})

test('cancellation during backoff prevents the next attempt', async () => {
  let calls = 0
  const controller = new AbortController()
  const { api } = harness(async () => { calls++; return json({}, 503) }, { retryDelays: [100, 100] })
  await assert.rejects(api.get('/read', {
    signal: controller.signal,
    onRetry: () => { setTimeout(() => controller.abort(), 5) },
  }), { name: 'AbortError' })
  assert.equal(calls, 1)
})

test('404 and validation errors are not retried and keep their details', async () => {
  for (const status of [400, 404, 422, 500]) {
    let calls = 0
    const { api } = harness(async () => { calls++; return json({ detail: 'Dataset not available' }, status) })
    await assert.rejects(api.get('/read'), { message: 'Dataset not available', status })
    assert.equal(calls, 1)
  }
})

test('a successful retry still validates its final HTTP error', async () => {
  let calls = 0
  const { api } = harness(async () => ++calls === 1 ? json({}, 503) : json({ detail: 'Missing artifact' }, 404))
  await assert.rejects(api.get('/read'), { message: 'Missing artifact', status: 404 })
  assert.equal(calls, 2)
})

test('a stalled non-JSON 404 body is not consumed or retried', async () => {
  let calls = 0
  let bodyReads = 0
  let requestSignal
  const { api } = harness(async (_url, { signal }) => {
    calls++
    requestSignal = signal
    const response = new Response(new ReadableStream(), {
      status: 404,
      headers: { 'Content-Type': 'text/html' },
    })
    response.text = () => { bodyReads++; return new Promise(() => {}) }
    return response
  }, { readTimeoutMs: 10 })
  await assert.rejects(api.get('/missing'), {
    status: 404,
    message: 'Request failed (404). Please try again.',
  })
  assert.equal(calls, 1)
  assert.equal(bodyReads, 0)
  assert.equal(requestSignal.aborted, true)
})

test('413 middleware and upload validation messages are preserved without retries', async () => {
  for (const payload of [
    { error: 'payload_too_large', message: 'request body exceeds maximum allowed size', max_bytes: 22020096 },
    { error: { message: 'Pilot version currently supports datasets up to 20 MB.' } },
  ]) {
    let calls = 0
    const { api } = harness(async () => { calls++; return json(payload, 413) })
    const expected = { status: 413, message: payload.message || payload.error.message, response: payload }
    await assert.rejects(api.postForm('/upload', new FormData()), expected)
    assert.equal(calls, 1)
    await assert.rejects(api.get('/read'), expected)
    assert.equal(calls, 2)
  }
})

test('POST never replays network failures, timeouts or transient HTTP errors', async () => {
  for (const failure of ['network', 'timeout', 502, 503, 504]) {
    let calls = 0
    const { api } = harness(async () => {
      calls++
      if (failure === 'network') throw new TypeError('Failed to fetch')
      if (failure === 'timeout') return new Promise(() => {})
      return json({}, failure)
    }, { readTimeoutMs: 5 })
    await assert.rejects(api.post('/write', { value: 1 }), { message: UNAVAILABLE_MESSAGE })
    assert.equal(calls, 1)
  }
})

test('upload uses a longer deadline, preserves multipart headers and reports uncertain failure', async () => {
  let calls = 0
  const form = new FormData()
  form.append('name', 'example')
  const { api } = harness(async (_url, init) => {
    calls++
    assert.equal(init.method, 'POST')
    assert.equal(init.body, form)
    assert.equal(init.headers['Content-Type'], undefined)
    await new Promise(resolve => setTimeout(resolve, 15))
    return json({ id: 'dataset' })
  }, { readTimeoutMs: 5, longTimeoutMs: 100 })
  assert.deepEqual(await api.postForm('/upload', form), { id: 'dataset' })
  assert.equal(calls, 1)
  const failed = harness(async () => { throw new TypeError('Failed to fetch') })
  await assert.rejects(failed.api.postForm('/upload', form), { message: UPLOAD_UNCERTAIN_MESSAGE })
})

test('upload deadline and caller cancellation never replay the file', async () => {
  let calls = 0
  const started = deferred()
  const { api } = harness(() => { calls++; started.resolve(); return new Promise(() => {}) })
  await assert.rejects(api.postForm('/upload', new FormData(), { timeoutMs: 5 }), { message: UPLOAD_UNCERTAIN_MESSAGE })
  assert.equal(calls, 1)
  const controller = new AbortController()
  const upload = api.postForm('/upload', new FormData(), { signal: controller.signal })
  await started.promise
  controller.abort()
  await assert.rejects(upload, { name: 'AbortError' })
  assert.ok(calls <= 2)
})

test('getBlob returns binary content after a transient retry', async () => {
  let calls = 0
  const { api } = harness(async () => ++calls === 1 ? json({}, 502) : new Response('pdf-data'))
  assert.equal(await (await api.getBlob('/summary.pdf')).text(), 'pdf-data')
  assert.equal(calls, 2)
})

test('concurrent 401 callers share one refresh, and retry results are both validated', async () => {
  const refreshing = deferred()
  const releaseRefresh = deferred()
  let refreshCalls = 0
  const { api, tokens } = harness(async (url, init) => {
    if (url.endsWith('/auth/refresh')) {
      refreshCalls++
      refreshing.resolve()
      await releaseRefresh.promise
      return json({ access_token: 'new' })
    }
    if (init.headers.Authorization === 'Bearer old') return json({}, 401)
    return url.endsWith('/missing') ? json({ detail: 'Not found' }, 404) : json({ ok: true })
  })
  const good = api.get('/good')
  const bad = assert.rejects(api.get('/missing'), { status: 404, message: 'Not found' })
  await refreshing.promise
  releaseRefresh.resolve()
  assert.deepEqual(await good, { ok: true })
  await bad
  assert.equal(refreshCalls, 1)
  assert.equal(tokens.get('access_token'), 'new')
})

test('one cancelled refresh waiter does not cancel another caller', async () => {
  const refreshing = deferred()
  const releaseRefresh = deferred()
  let refreshCalls = 0
  const { api } = harness(async (url, init) => {
    if (url.endsWith('/auth/refresh')) {
      refreshCalls++
      refreshing.resolve()
      await releaseRefresh.promise
      return json({ access_token: 'new' })
    }
    return init.headers.Authorization === 'Bearer old' ? json({}, 401) : json({ ok: true })
  })
  const controller = new AbortController()
  const cancelled = assert.rejects(api.get('/read', { signal: controller.signal }), { name: 'AbortError' })
  const completed = api.get('/read')
  await refreshing.promise
  controller.abort()
  await cancelled
  releaseRefresh.resolve()
  assert.deepEqual(await completed, { ok: true })
  assert.equal(refreshCalls, 1)
})

test('GET retry after refresh uses the new token without refreshing again', async () => {
  let refreshCalls = 0
  let authorizedReads = 0
  const { api } = harness(async (url, init) => {
    if (url.endsWith('/auth/refresh')) {
      refreshCalls++
      return json({ access_token: 'new' })
    }
    if (init.headers.Authorization === 'Bearer old') return json({}, 401)
    return ++authorizedReads === 1 ? json({}, 503) : json({ ok: true })
  })
  assert.deepEqual(await api.get('/read'), { ok: true })
  assert.equal(refreshCalls, 1)
  assert.equal(authorizedReads, 2)
})

test('refresh outages retain credentials and never redirect or replay refresh POST', async () => {
  for (const failure of ['network', 'timeout', 503]) {
    let refreshCalls = 0
    const h = harness(async url => {
      if (!url.endsWith('/auth/refresh')) return json({}, 401)
      refreshCalls++
      if (failure === 'network') throw new TypeError('Failed to fetch')
      if (failure === 'timeout') return new Promise(() => {})
      return json({}, failure)
    }, { readTimeoutMs: 15 })
    await assert.rejects(h.api.get('/read'), { message: UNAVAILABLE_MESSAGE })
    assert.equal(h.logouts, 0)
    assert.equal(h.tokens.get('refresh_token'), 'refresh')
    assert.equal(refreshCalls, 1)
  }
})

test('explicit invalid refresh expires the session', async () => {
  const h = harness(async () => json({ detail: 'Session expired' }, 401))
  await assert.rejects(h.api.get('/read'), { status: 401 })
  assert.equal(h.logouts, 1)
  assert.equal(h.tokens.size, 0)
})

test('POST retains the explicit 401 refresh handshake and validates the resubmission', async () => {
  let writes = 0
  const { api } = harness(async url => {
    if (url.endsWith('/auth/refresh')) return json({ access_token: 'new' })
    return ++writes === 1 ? json({}, 401) : json({ detail: 'Invalid payload' }, 422)
  })
  await assert.rejects(api.post('/write'), { status: 422, message: 'Invalid payload' })
  assert.equal(writes, 2)
})

test('SSE preserves split frames and malformed-frame skipping', async () => {
  let calls = 0
  const encoder = new TextEncoder()
  const { api } = harness(async () => {
    calls++
    return new Response(new ReadableStream({
      start(controller) {
        for (const chunk of ['event: token\ndata: {"text":', '"hi"}\n\nevent: bad\ndata: nope\n\n', 'event: done\ndata: {}\n\n']) {
          controller.enqueue(encoder.encode(chunk))
        }
        controller.close()
      },
    }))
  })
  const frames = []
  for await (const frame of api.streamSSE('/chat')) frames.push(frame)
  assert.deepEqual(frames, [{ event: 'token', data: { text: 'hi' } }, { event: 'done', data: {} }])
  assert.equal(calls, 1)
})

test('SSE never retries connection errors or transient responses', async () => {
  for (const failure of ['network', 503]) {
    let calls = 0
    const { api } = harness(async () => {
      calls++
      if (failure === 'network') throw new TypeError('Failed to fetch')
      return json({}, failure)
    })
    await assert.rejects(api.streamSSE('/chat').next(), { message: UNAVAILABLE_MESSAGE })
    assert.equal(calls, 1)
  }
})

test('SSE stalled body times out and cancels the reader without reconnecting', async () => {
  let calls = 0
  let cancelled = 0
  const { api } = harness(async () => {
    calls++
    return new Response(new ReadableStream({ cancel() { cancelled++ } }))
  }, { longTimeoutMs: 5 })
  await assert.rejects(api.streamSSE('/chat').next(), { message: UNAVAILABLE_MESSAGE })
  assert.equal(calls, 1)
  assert.equal(cancelled, 1)
})

test('SSE honors explicit cancellation while reading', async () => {
  const started = deferred()
  const { api } = harness(async () => new Response(new ReadableStream({ pull() { started.resolve() } })))
  const controller = new AbortController()
  const result = api.streamSSE('/chat', {}, { signal: controller.signal }).next()
  await started.promise
  controller.abort()
  await assert.rejects(result, { name: 'AbortError' })
})
