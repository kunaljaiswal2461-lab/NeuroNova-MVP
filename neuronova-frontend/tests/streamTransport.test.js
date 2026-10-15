import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '../src/api/request'

const encoder = new TextEncoder()

function jwt(expSecondsFromNow) {
  const payload = btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + expSecondsFromNow }))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `header.${payload}.signature`
}

function harness(fetchImpl, { access = 'old', refresh = 'refresh', ...config } = {}) {
  const tokens = new Map()
  if (access) tokens.set('access_token', access)
  if (refresh) tokens.set('refresh_token', refresh)
  const logouts = vi.fn()
  const api = createApi({
    baseUrl: 'https://api.example.test',
    fallbackApiKey: 'key',
    storage: {
      getItem: key => tokens.get(key) ?? null,
      setItem: (key, value) => tokens.set(key, value),
      removeItem: key => tokens.delete(key),
    },
    onAuthExpired: logouts,
    fetchImpl,
    ...config,
  })
  return { api, tokens, logouts }
}

function streamOf(chunks) {
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  }))
}

async function collect(iterator) {
  const frames = []
  for await (const frame of iterator) frames.push(frame)
  return frames
}

describe('SSE parser', () => {
  it('parses CRLF framing like LF framing (E13)', async () => {
    const { api } = harness(async () => streamOf(['event: token\r\ndata: {"text":"hi"}\r\n\r\nevent: done\r\ndata: {}\r\n\r\n']))
    expect(await collect(api.streamSSE('/chat'))).toEqual([{ event: 'token', data: { text: 'hi' } }, { event: 'done', data: {} }])
  })

  it('keeps a CRLF split across two chunks together', async () => {
    const { api } = harness(async () => streamOf(['event: token\r\ndata: {"a":1}\r', '\n\r', '\nevent: done\r\ndata: {}\r\n\r\n']))
    expect(await collect(api.streamSSE('/chat'))).toEqual([{ event: 'token', data: { a: 1 } }, { event: 'done', data: {} }])
  })

  it('treats a bare CR as a line break', async () => {
    const { api } = harness(async () => streamOf(['event: done\rdata: {}\r\r']))
    expect(await collect(api.streamSSE('/chat'))).toEqual([{ event: 'done', data: {} }])
  })

  it('parses a final frame that has no trailing blank line (E12)', async () => {
    const { api } = harness(async () => streamOf(['event: token\ndata: {"text":"x"}\n\nevent: done\ndata: {}']))
    expect(await collect(api.streamSSE('/chat'))).toEqual([{ event: 'token', data: { text: 'x' } }, { event: 'done', data: {} }])
  })

  it('flushes a multi-byte character split at the end of the stream', async () => {
    const bytes = encoder.encode('event: token\ndata: {"text":"₹"}')
    const { api } = harness(async () => new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(bytes.slice(0, bytes.length - 3))
        controller.enqueue(bytes.slice(bytes.length - 3))
        controller.close()
      },
    })))
    expect(await collect(api.streamSSE('/chat'))).toEqual([{ event: 'token', data: { text: '₹' } }])
  })

  it('joins multi-line data fields with a newline before parsing', async () => {
    const { api } = harness(async () => streamOf(['event: token\ndata: {"text":\ndata: "two lines"}\n\n']))
    expect(await collect(api.streamSSE('/chat'))).toEqual([{ event: 'token', data: { text: 'two lines' } }])
  })

  it('skips an invalid-JSON frame, counts it, and keeps going (E14)', async () => {
    const { api } = harness(async () => streamOf(['event: bad\ndata: nope\n\nevent: done\ndata: {}\n\n']))
    const stream = api.streamSSE('/chat')
    expect(await collect(stream)).toEqual([{ event: 'done', data: {} }])
    expect(stream.skippedFrames).toBe(1)
  })

  it('ignores comment frames and passes an unknown event through for the caller to ignore (E15)', async () => {
    const { api } = harness(async () => streamOf([': keepalive\n\n', 'event: surprise\ndata: {"x":1}\n\n', ': keepalive\n\n']))
    expect(await collect(api.streamSSE('/chat'))).toEqual([{ event: 'surprise', data: { x: 1 } }])
  })
})

describe('stream deadlines', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  function controlled() {
    let controller
    const body = new ReadableStream({ start(c) { controller = c } })
    return { body, push: text => controller.enqueue(encoder.encode(text)), close: () => controller.close() }
  }

  it('keeps a long turn alive while heartbeats arrive, then completes (E1)', async () => {
    const source = controlled()
    const { api } = harness(async () => new Response(source.body))
    const frames = collect(api.streamSSE('/chat'))
    await vi.advanceTimersByTimeAsync(0)
    for (let second = 0; second < 140; second += 10) {
      source.push(': keepalive\n\n')
      await vi.advanceTimersByTimeAsync(10_000)
    }
    source.push('event: done\ndata: {}\n\n')
    source.close()
    expect(await frames).toEqual([{ event: 'done', data: {} }])
  })

  it('aborts with stream_idle_timeout after 45 s without bytes (E2)', async () => {
    const source = controlled()
    const { api } = harness(async () => new Response(source.body))
    const result = collect(api.streamSSE('/chat')).catch(error => error)
    await vi.advanceTimersByTimeAsync(44_000)
    source.push(': keepalive\n\n')                         // a comment is bytes: it resets the idle timer
    await vi.advanceTimersByTimeAsync(44_000)
    await vi.advanceTimersByTimeAsync(2_000)
    const error = await result
    expect(error.code).toBe('stream_idle_timeout')
    expect(error.name).toBe('TimeoutError')
  })

  it('aborts with connect_timeout when headers never arrive (E6)', async () => {
    const { api } = harness(() => new Promise(() => {}))
    const result = collect(api.streamSSE('/chat')).catch(error => error)
    await vi.advanceTimersByTimeAsync(74_000)
    let settled = false
    result.then(() => { settled = true })
    await vi.advanceTimersByTimeAsync(0)
    expect(settled).toBe(false)                            // a 55 s cold start is still waiting (E5)
    await vi.advanceTimersByTimeAsync(2_000)
    expect((await result).code).toBe('connect_timeout')
  })

  it('waits through a 55 s cold start and then streams (E5)', async () => {
    const source = controlled()
    const { api } = harness(() => new Promise(resolve => setTimeout(() => resolve(new Response(source.body)), 55_000)))
    const frames = collect(api.streamSSE('/chat'))
    await vi.advanceTimersByTimeAsync(56_000)
    source.push('event: done\ndata: {}\n\n')
    source.close()
    expect(await frames).toEqual([{ event: 'done', data: {} }])
  })

  it('stops at the hard ceiling even when heartbeats keep arriving', async () => {
    const source = controlled()
    const { api } = harness(async () => new Response(source.body), { maxStreamMs: 60_000 })
    const result = collect(api.streamSSE('/chat')).catch(error => error)
    await vi.advanceTimersByTimeAsync(0)
    for (let i = 0; i < 5; i++) {
      source.push(': keepalive\n\n')
      await vi.advanceTimersByTimeAsync(10_000)
    }
    source.push(': keepalive\n\n')
    await vi.advanceTimersByTimeAsync(11_000)
    expect((await result).code).toBe('stream_max_timeout')
  })

  it('lets a caller override each limit through options', async () => {
    const { api } = harness(() => new Promise(() => {}))
    const result = collect(api.streamSSE('/chat', {}, { connectTimeoutMs: 1000 })).catch(error => error)
    await vi.advanceTimersByTimeAsync(1500)
    expect((await result).code).toBe('connect_timeout')
  })
})

describe('ensureFreshToken', () => {
  it('does not refresh a token with plenty of validity left', async () => {
    const fetchImpl = vi.fn()
    const { api } = harness(fetchImpl, { access: jwt(600) })
    await api.ensureFreshToken()
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('refreshes a token that expires in 30 s before the send (E7)', async () => {
    const fresh = jwt(3600)
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ access_token: fresh }), { status: 200, headers: { 'content-type': 'application/json' } }))
    const { api, tokens } = harness(fetchImpl, { access: jwt(30) })
    await api.ensureFreshToken()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(tokens.get('access_token')).toBe(fresh)
  })

  it('refreshes a token it cannot read', async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ access_token: jwt(3600) }), { status: 200, headers: { 'content-type': 'application/json' } }))
    const { api } = harness(fetchImpl, { access: 'not-a-jwt' })
    await api.ensureFreshToken()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('goes through onAuthExpired and throws when the refresh is rejected (E8)', async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ error: { message: 'expired' } }), { status: 401, headers: { 'content-type': 'application/json' } }))
    const { api, logouts } = harness(fetchImpl, { access: jwt(5) })
    await expect(api.ensureFreshToken()).rejects.toMatchObject({ status: 401 })
    expect(logouts).toHaveBeenCalledTimes(1)
  })

  it('does not block the send on a transient refresh failure', async () => {
    const fetchImpl = vi.fn(async () => { throw new TypeError('Failed to fetch') })
    const { api, logouts, tokens } = harness(fetchImpl, { access: jwt(5) })
    await expect(api.ensureFreshToken()).resolves.toBeUndefined()
    expect(logouts).not.toHaveBeenCalled()
    expect(tokens.get('refresh_token')).toBe('refresh')
  })

  it('does nothing without a refresh token or without an access token', async () => {
    const fetchImpl = vi.fn()
    await harness(fetchImpl, { access: jwt(1), refresh: null }).api.ensureFreshToken()
    await harness(fetchImpl, { access: null }).api.ensureFreshToken()
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('still never replays a stream after a 401 response', async () => {
    const calls = []
    const fetchImpl = vi.fn(async url => {
      calls.push(url)
      return new Response(JSON.stringify({ error: { message: 'no' } }), { status: 401, headers: { 'content-type': 'application/json' } })
    })
    const { api } = harness(fetchImpl, { access: jwt(600) })
    await expect(collect(api.streamSSE('/chat'))).rejects.toMatchObject({ status: 401 })
    expect(calls).toEqual(['https://api.example.test/chat'])
  })
})
