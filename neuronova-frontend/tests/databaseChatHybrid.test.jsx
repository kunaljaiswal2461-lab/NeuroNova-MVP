// SPEC-09 L1 frontend 1: the database chat consumes every C1 path, and reloads a stored SQL turn.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import DatabaseChat from '../src/pages/DatabaseChat'

vi.mock('../src/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), streamSSE: vi.fn() },
}))
vi.mock('../src/components/Layout', () => ({ default: ({ children }) => <main>{children}</main> }))

const sourceId = 'source-1'
const session = { session_id: 'source-session-1', source_id: sourceId, dataset_id: null, mode: 'AUTO', message_count: 0 }
const NO = { x: null, y: [], series: null, label: null, value: null }

const result = {
  execution_id: 'abcdef12-0000-0000-0000-000000000000',
  status: 'succeeded',
  source_id: sourceId,
  snapshot_id: 'snapshot-1',
  columns: [{ alias: 'n', logical_type: 'integer', expected_postgres_type: 'int8', actual_postgres_type: 'int8' }],
  rows: [[{ kind: 'integer', value: '42' }]],
  returned_row_count: 1,
  complete: true,
  duration_ms: 3,
  session_time_zone: 'UTC',
  warnings: [],
  ordering: [],
}
const presentation = {
  answer: 'There are 42 orders.', takeaways: ['One row'], caveats: ['The AI model did not see row values for this answer (source setting).'],
  display: 'kpi', encodings: { ...NO, value: 'n' }, table_columns: [], fallback: null, derived: {},
}

function renderChat() {
  return render(
    <MemoryRouter initialEntries={[`/sources/${sourceId}/chat`]}>
      <Routes><Route path="/sources/:sourceId/chat" element={<DatabaseChat />} /></Routes>
    </MemoryRouter>,
  )
}

function configure({ sessions = [], messages = [] } = {}) {
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}`) return Promise.resolve({ source_id: sourceId, display_name: 'Shop' })
    if (path === `/sources/${sourceId}/catalog`) return Promise.resolve({ source_id: sourceId, current_snapshot_id: 'snapshot-1' })
    if (path === `/sources/${sourceId}/chat/sessions?limit=50`) return Promise.resolve({ sessions, count: sessions.length })
    if (path === '/chat/sessions/source-session-1/messages?limit=500') return Promise.resolve({ session_id: 'source-session-1', messages, count: messages.length })
    throw new Error(`unexpected GET ${path}`)
  })
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/chat/sessions`) return Promise.resolve({ session })
    throw new Error(`unexpected POST ${path}`)
  })
}

async function ask(question = 'How many orders are there?') {
  const composer = await screen.findByRole('textbox', { name: 'Database question' })
  await waitFor(() => expect(composer.disabled).toBe(false))
  fireEvent.change(composer, { target: { value: question } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))
}

beforeEach(() => {
  window.sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  configure()
})
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('SQL answer: shows the activity while it runs, then the answer, the SQL and the table behind the display', async () => {
  let release
  const gate = new Promise(resolve => { release = resolve })
  async function* stream() {
    yield { event: 'start', data: { turn_id: 'turn-1' } }
    yield { event: 'planning', data: { state: 'planning' } }
    yield { event: 'sql_generated', data: { sql: 'SELECT count(*) AS n FROM sales.orders', assumptions: ['All time'], output_intent: null, rewrites: [], attempt: 1 } }
    yield { event: 'executing', data: { state: 'executing' } }
    await gate
    yield { event: 'postgres_result', data: { postgres_result: result } }
    yield { event: 'presentation', data: presentation }
    yield { event: 'token', data: { text: presentation.answer } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()

  await screen.findByText('Running the query on Shop…')
  release()
  expect((await screen.findByTestId('answer-text')).textContent).toBe('There are 42 orders.')
  expect(screen.getAllByText('There are 42 orders.')).toHaveLength(1)                      // not duplicated by the token text
  expect(screen.getByTestId('kpi-value').textContent).toBe('42')
  expect(screen.getByText('All time')).toBeTruthy()
  expect(screen.getByText('SQL used')).toBeTruthy()
  expect(document.querySelector('pre code').textContent).toBe('SELECT count(*) AS n FROM sales.orders')
  expect(screen.getByRole('list', { name: 'Takeaways' }).textContent).toContain('One row')
  expect(screen.getByText(/did not see row values/)).toBeTruthy()
})

it('shows the "query written" activity after sql_generated and the repair activity after repairing', async () => {
  const gates = [] 
  const wait = () => new Promise(resolve => gates.push(resolve))
  async function* stream() {
    yield { event: 'sql_generated', data: { sql: 'SELECT cnt FROM sales.orders', assumptions: [], attempt: 1 } }
    await wait()
    yield { event: 'repairing', data: { reason_code: 'column_not_found' } }
    await wait()
    yield { event: 'sql_generated', data: { sql: 'SELECT count(*) AS n FROM sales.orders', assumptions: [], attempt: 2 } }
    yield { event: 'executing', data: { state: 'executing' } }
    yield { event: 'postgres_result', data: { postgres_result: result } }
    yield { event: 'presentation', data: presentation }
    yield { event: 'token', data: { text: presentation.answer } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  await screen.findByText('Query written · checking it…')
  gates[0]()
  await screen.findByText('The first query failed (a column name didn\'t match) · correcting it once…')
  gates[1]()
  await screen.findByTestId('answer-text')
  expect(screen.getByText('Corrected automatically: a column name didn\'t match')).toBeTruthy()
  expect(document.querySelector('pre code').textContent).toBe('SELECT count(*) AS n FROM sales.orders')   // the corrected query
})

it('a failed SQL turn shows the fixed message and no result', async () => {
  async function* stream() {
    yield { event: 'sql_generated', data: { sql: 'SELECT 1', assumptions: [], attempt: 1 } }
    yield { event: 'executing', data: {} }
    yield { event: 'error', data: { code: 'execution_timeout', message: 'The calculation took too long. Add a time range or a filter and ask again.' } }
    yield { event: 'token', data: { text: 'The calculation took too long. Add a time range or a filter and ask again.' } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  expect(await screen.findByText(/The controlled database query timed out|took too long/)).toBeTruthy()
  expect(screen.queryByText('SQL used')).toBeNull()
  expect(screen.queryByTestId('answer-text')).toBeNull()
})

it('a model_unavailable error shows the backend message', async () => {
  async function* stream() {
    yield { event: 'error', data: { code: 'model_unavailable', message: 'The assistant model is not configured.' } }
    yield { event: 'token', data: { text: 'The assistant model is not configured.' } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  expect(await screen.findByText('The assistant model is not configured.')).toBeTruthy()
})

it.each([
  ['clarification', 'Which year do you mean?', 'Reply below — your reply is read together with this question.'],
  ['unsupported', 'Joins across these tables are not supported.', 'Try asking for a read-only summary of the data instead.'],
])('%s ends the turn with its text and no SQL panel', async (event, message, badge) => {
  async function* stream() {
    yield { event, data: { message, outcome: event } }
    yield { event: 'token', data: { text: message } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  expect(await screen.findByText(message)).toBeTruthy()
  expect(screen.getByText(badge)).toBeTruthy()
  expect(screen.queryByText('SQL used')).toBeNull()
})

it('the typed path is unchanged: a result without a presentation renders the controlled result table', async () => {
  async function* stream() {
    yield { event: 'postgres_result', data: { postgres_result: result } }
    yield { event: 'token', data: { text: 'The read-only calculation completed with 1 returned row(s).' } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  expect(await screen.findByLabelText('Controlled PostgreSQL result')).toBeTruthy()
  expect(screen.getByText('The read-only calculation completed with 1 returned row(s).')).toBeTruthy()
  expect(screen.queryByRole('region', { name: 'Answer' })).toBeNull()
})

it('reloads a stored SQL turn from nl_sql with the same fields', async () => {
  const stored = [
    { message_id: 'm1', role: 'USER', content: 'How many orders?', created_at: '2026-10-12T10:00:00Z' },
    {
      message_id: 'm2', role: 'ASSISTANT', content: 'There are 42 orders.', created_at: '2026-10-12T10:00:01Z',
      postgres_result: result,
      nl_sql: {
        version: 1, route: 'sql', sql: 'SELECT count(*) AS n FROM sales.orders', assumptions: ['All time'],
        repair: { reason_code: 'column_not_found', original_sql: 'SELECT cnt FROM sales.orders', attempts: 1 },
        outcome: 'answer', presentation: { ...presentation, answer_template: 'x', takeaways_template: [], pass2: null },
      },
    },
  ]
  configure({ sessions: [{ ...session, message_count: 2 }], messages: stored })
  renderChat()
  expect((await screen.findByTestId('answer-text')).textContent).toBe('There are 42 orders.')
  expect(screen.getByTestId('kpi-value').textContent).toBe('42')
  expect(screen.getByText('All time')).toBeTruthy()
  expect(screen.getByText(/^Corrected automatically/)).toBeTruthy()
  expect(document.querySelector('pre code').textContent).toBe('SELECT count(*) AS n FROM sales.orders')
})

it('reloads a typed-path turn without nl_sql exactly as before', async () => {
  const stored = [
    { message_id: 'm1', role: 'USER', content: 'net revenue', created_at: '2026-10-12T10:00:00Z' },
    { message_id: 'm2', role: 'ASSISTANT', content: 'The read-only calculation completed with 1 returned row(s).', created_at: '2026-10-12T10:00:01Z', postgres_result: result, nl_sql: null },
  ]
  configure({ sessions: [{ ...session, message_count: 2 }], messages: stored })
  renderChat()
  expect(await screen.findByLabelText('Controlled PostgreSQL result')).toBeTruthy()
  expect(screen.queryByText('SQL used')).toBeNull()
})

it('a write refusal has no hint (E21)', async () => {
  const message = 'This assistant only reads data. It cannot change data or the database structure.'
  async function* stream() {
    yield { event: 'unsupported', data: { message, outcome: 'unsupported' } }
    yield { event: 'token', data: { text: message } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask('delete all orders')
  expect(await screen.findByText(message)).toBeTruthy()
  expect(screen.queryByText(/Try asking for a read-only summary/)).toBeNull()
})
