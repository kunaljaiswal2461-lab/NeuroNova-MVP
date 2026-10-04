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

  const composer = screen.getByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'What is the total?' } })
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

it('switches an eligible returned result between its exact table and a local chart without another request', async () => {
  const chartableResult = {
    ...result,
    complete: true,
    safety_row_truncated: false,
    explicit_top_n: true,
    requested_limit: 2,
    columns: [
      { alias: 'region', logical_type: 'text' },
      { alias: 'revenue', logical_type: 'decimal' },
    ],
    rows: [
      [{ kind: 'text', value: 'East' }, { kind: 'decimal', value: '-5.00' }],
      [{ kind: 'text', value: 'West' }, { kind: 'decimal', value: '10.25' }],
    ],
  }
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: chartableResult } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'Show revenue by region' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  await screen.findByText('-5.00')
  expect(screen.getByRole('tab', { name: 'Table' }).getAttribute('aria-selected')).toBe('true')
  fireEvent.click(screen.getByRole('tab', { name: 'Chart' }))
  expect(await screen.findByRole('tabpanel', { name: 'Database result chart' })).toBeTruthy()
  expect(screen.getByText(/Requested Top-2 result/)).toBeTruthy()
  expect(api.streamSSE).toHaveBeenCalledTimes(1)

  fireEvent.click(screen.getByRole('tab', { name: 'Table' }))
  expect(await screen.findByText('-5.00')).toBeTruthy()
})

it('explains why repeated categories remain table-only', async () => {
  const ambiguousResult = {
    ...result,
    complete: true,
    safety_row_truncated: false,
    columns: [{ alias: 'region', logical_type: 'text' }, { alias: 'revenue', logical_type: 'decimal' }],
    rows: [
      [{ kind: 'text', value: 'East' }, { kind: 'decimal', value: '1' }],
      [{ kind: 'text', value: 'East' }, { kind: 'decimal', value: '2' }],
    ],
  }
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: ambiguousResult } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'Show revenue by region' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  expect(await screen.findByText(/repeated category labels would make the bars ambiguous/i)).toBeTruthy()
  expect(screen.queryByRole('tab', { name: 'Chart' })).toBeNull()
  expect(screen.getByRole('tabpanel', { name: 'Database result table' })).toBeTruthy()
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

it('continues source-bound session discovery after a stale saved session returns 404', async () => {
  window.sessionStorage.setItem('neuronova:source-chat-session:v1:source-1', 'stale-session')
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}`) return Promise.resolve({ source_id: sourceId, display_name: 'Production analytics' })
    if (path === `/sources/${sourceId}/catalog`) return Promise.resolve({ source_id: sourceId, current_snapshot_id: 'snapshot-1' })
    if (path === '/chat/sessions/stale-session') return Promise.reject(Object.assign(new Error('session not found'), { status: 404 }))
    if (path === `/sources/${sourceId}/chat/sessions?limit=50`) return Promise.resolve({ sessions: [], count: 0 })
    throw new Error(`unexpected GET ${path}`)
  })
  renderDatabaseChat()

  await screen.findByText('Production analytics')
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${sourceId}/chat/sessions`,
    { mode: 'AUTO' },
    expect.objectContaining({ requireJwt: true, preserveErrorBody: true }),
  ))
  expect(window.sessionStorage.getItem('neuronova:source-chat-session:v1:source-1')).toBe(session.session_id)
})

it('does not duplicate a terminal clarification when its matching token arrives', async () => {
  const clarification = 'Which date range should I use?'
  async function* stream() {
    yield { event: 'clarification', data: { message: clarification } }
    yield { event: 'token', data: { text: clarification } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'Show revenue' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  const clarificationNode = await screen.findByText((content, node) => node?.tagName === 'P' && content === clarification)
  expect(clarificationNode.textContent).toBe(clarification)
  expect(screen.getByText('Clarification required')).toBeTruthy()
})

it('retains a delivered PostgreSQL result when the stream ends with an error event', async () => {
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: result } }
    yield { event: 'error', data: { code: 'execution_cancelled', message: 'The query was cancelled.' } }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'What is the total?' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  await screen.findByText('12345678901234567890.01')
  expect(await screen.findByText('The database result above was received, but the response ended before its final summary.')).toBeTruthy()
  expect(screen.queryByText('The database query was cancelled.')).toBeNull()
})

it('retains a delivered PostgreSQL result when the stream aborts after delivery', async () => {
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: result } }
    throw new DOMException('Request interrupted.', 'AbortError')
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'What is the total?' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  await screen.findByText('12345678901234567890.01')
  expect(await screen.findByText('The database result above was received before the response ended.')).toBeTruthy()
  expect(screen.queryByText('The database query was cancelled.')).toBeNull()
})

it('does not offer cancellation after a PostgreSQL result has been delivered', async () => {
  let releaseStream
  const waitForDone = new Promise(resolve => { releaseStream = resolve })
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: result } }
    await waitForDone
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'What is the total?' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  await screen.findByText('12345678901234567890.01')
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull())
  releaseStream()
})

it('labels requested Top-N separately from safety truncation', async () => {
  const topNResult = {
    ...result,
    complete: true,
    safety_row_truncated: false,
    explicit_top_n: true,
    requested_limit: 5,
  }
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: topNResult } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'Show the top five customers' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  expect(await screen.findByText(/This shows the requested top 5 results/)).toBeTruthy()
  expect(screen.getByText(/This question limit is not a safety truncation/)).toBeTruthy()
  expect(screen.queryByText(/This result is partial/)).toBeNull()
})

it('does not describe a zero-row safety-truncated result as no matches', async () => {
  const byteTruncatedResult = {
    ...result,
    rows: [],
    returned_row_count: 0,
    complete: false,
    safety_row_truncated: false,
    safety_byte_truncated: true,
  }
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: byteTruncatedResult } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'List results' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  expect(await screen.findByText(/No result rows could be safely displayed before a safety limit was reached/)).toBeTruthy()
  expect(screen.queryByText('No rows matched this query.')).toBeNull()
})

it('renders safe confirmed metric provenance when the controlled result supplies it', async () => {
  const metricResult = {
    ...result,
    metric_provenance: {
      metric_id: 'metric-1',
      version_number: 2,
      name: 'Confirmed revenue',
      aggregation: 'sum',
      aggregation_column_name: 'amount',
      unit_label: 'USD',
      default_date_column_name: 'created_at',
      default_time_zone: 'UTC',
      fixed_filter_count: 2,
    },
  }
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: metricResult } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'What is confirmed revenue?' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  expect(await screen.findByLabelText('Confirmed metric provenance')).toBeTruthy()
  expect(screen.getByText('Confirmed revenue · version 2')).toBeTruthy()
  expect(screen.getByText('Sum · amount')).toBeTruthy()
  expect(screen.getByText('Unit: USD · Time basis: created_at · Time zone: UTC')).toBeTruthy()
  expect(screen.getByText('2 fixed filters applied')).toBeTruthy()
})

it('renders approved relationship semantics without exposing join conditions or values', async () => {
  const joinedResult = {
    ...result,
    relationship_provenance: {
      relationship_id: 'relationship-1',
      relationship_version_id: 'relationship-version-3',
      version_number: 3,
      join_type: 'left',
    },
    calculation_semantics: {
      join_type: 'left',
      left_join_preserves_unmatched_base_rows: true,
      lookup_filters_in_on: true,
      lookup_filters_in_where: false,
    },
  }
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: joinedResult } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderDatabaseChat()

  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  fireEvent.change(composer, { target: { value: 'Show revenue by customer region' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))

  expect(await screen.findByLabelText('Approved relationship provenance')).toBeTruthy()
  expect(screen.getByText('Left join · version 3')).toBeTruthy()
  expect(screen.getByText(/Lookup filters were applied in the join condition/)).toBeTruthy()
  expect(screen.queryByText('relationship-1')).toBeNull()
  expect(screen.queryByText('relationship-version-3')).toBeNull()
})
