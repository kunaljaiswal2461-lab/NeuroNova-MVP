import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import DatabaseChat from '../src/pages/DatabaseChat'

vi.mock('../src/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), streamSSE: vi.fn() },
}))

vi.mock('../src/components/Layout', () => ({
  default: ({ children }) => <main>{children}</main>,
}))

const sourceId = 'source-1'
const session = {
  session_id: 'source-session-1',
  source_id: sourceId,
  dataset_id: null,
  mode: 'AUTO',
  message_count: 0,
}

const result = {
  execution_id: 'execution-1',
  status: 'succeeded',
  source_id: sourceId,
  snapshot_id: 'snapshot-1',
  columns: [
    { alias: 'total', logical_type: 'decimal' },
    { alias: 'customer_id', logical_type: 'integer' },
  ],
  rows: [[
    { kind: 'decimal', value: '12345678901234567890.01' },
    { kind: 'integer', value: '9007199254740993' },
  ]],
  returned_row_count: 1,
  complete: false,
  safety_row_truncated: true,
  safety_byte_truncated: false,
  ordering: [{ alias: 'total', direction: 'desc' }],
  session_time_zone: 'UTC',
  warnings: ['Showing a bounded result.'],
  duration_ms: 12.5,
}

function renderDatabaseChat() {
  return render(
    <MemoryRouter initialEntries={[`/sources/${sourceId}/chat`]}>
      <Routes><Route path="/sources/:sourceId/chat" element={<DatabaseChat />} /></Routes>
    </MemoryRouter>,
  )
}

function configureSourceRequests(onCancel) {
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}`) return Promise.resolve({ source_id: sourceId, display_name: 'Production analytics' })
    if (path === `/sources/${sourceId}/catalog`) return Promise.resolve({ source_id: sourceId, current_snapshot_id: 'snapshot-1' })
    if (path === `/sources/${sourceId}/chat/sessions?limit=50`) return Promise.resolve({ sessions: [], count: 0 })
    throw new Error(`unexpected GET ${path}`)
  })
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/chat/sessions`) return Promise.resolve({ session })
    if (path === `/chat/sessions/${session.session_id}/turns/turn-1/cancel`) {
      onCancel?.()
      return Promise.resolve(null)
    }
    throw new Error(`unexpected POST ${path}`)
  })
}

beforeEach(() => {
  window.sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  configureSourceRequests()
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('creates a JWT-only source session and renders precision-safe PostgreSQL values', async () => {
  async function* stream() {
    yield { event: 'planning', data: { message: 'Choosing an authorized table…' } }
    yield { event: 'executing', data: { message: 'Executing controlled read-only query…' } }
    yield { event: 'postgres_result', data: { postgres_result: result } }
    yield { event: 'token', data: { text: 'The requested total is shown below.' } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  await screen.findByText('Production analytics')
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${sourceId}/chat/sessions`,
    { mode: 'AUTO' },
    expect.objectContaining({ requireJwt: true, preserveErrorBody: true }),
  ))
  expect(api.post.mock.calls.some(([path]) => path.startsWith('/datasets/'))).toBe(false)

  fireEvent.change(screen.getByPlaceholderText('Ask a question about this database source…'), { target: { value: 'What is the total?' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  await screen.findByText('12345678901234567890.01')
  expect(screen.getByText('9007199254740993')).toBeTruthy()
  expect(screen.getByText(/This result is partial/)).toBeTruthy()
  expect(screen.getByText('The requested total is shown below.')).toBeTruthy()
  expect(api.streamSSE).toHaveBeenCalledWith(
    '/chat/sessions/source-session-1/message',
    { message: 'What is the total?' },
    expect.objectContaining({ requireJwt: true, preserveErrorBody: true }),
  )
})

it('does not create a source session when current metadata is unavailable', async () => {
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}`) return Promise.resolve({ source_id: sourceId, display_name: 'Production analytics' })
    if (path === `/sources/${sourceId}/catalog`) return Promise.resolve({ source_id: sourceId, current_snapshot_id: null })
    throw new Error(`unexpected GET ${path}`)
  })
  renderDatabaseChat()

  await screen.findByText(/needs published metadata/i)
  expect(api.post).not.toHaveBeenCalled()
  expect(api.streamSSE).not.toHaveBeenCalled()
})

it('aborts a source stream on explicit cancellation without replaying the message', async () => {
  let streamOptions
  let signalAtCancellation
  async function* cancellableStream(options) {
    yield { event: 'start', data: { turn_id: 'turn-1' } }
    await new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('Request cancelled.', 'AbortError')), { once: true })
    })
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation((_path, _body, options) => {
    streamOptions = options
    return cancellableStream(options)
  })
  configureSourceRequests(() => { signalAtCancellation = streamOptions.signal.aborted })
  renderDatabaseChat()

  await screen.findByPlaceholderText('Ask a question about this database source…')
  fireEvent.change(screen.getByPlaceholderText('Ask a question about this database source…'), { target: { value: 'Count rows' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))
  await screen.findByRole('button', { name: 'Cancel' })
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

  await screen.findByText('The database query was cancelled.')
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/chat/sessions/${session.session_id}/turns/turn-1/cancel`,
    {},
    expect.objectContaining({ requireJwt: true, preserveErrorBody: true, expectEmptyResponse: true }),
  ))
  expect(signalAtCancellation).toBe(false)
  expect(streamOptions.signal.aborted).toBe(true)
  expect(api.streamSSE).toHaveBeenCalledTimes(1)
})
