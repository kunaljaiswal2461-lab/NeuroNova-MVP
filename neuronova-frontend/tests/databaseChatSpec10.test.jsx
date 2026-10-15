// SPEC-10 W1.6/W1.7/W2/W5/W6 on the database chat: notices, reconcile, cards, new conversation, scroll, composer.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import DatabaseChat from '../src/pages/DatabaseChat'

vi.mock('../src/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(), streamSSE: vi.fn() } }))
vi.mock('../src/components/Layout', () => ({ default: ({ children }) => <main>{children}</main> }))

const sourceId = 'source-1'
const session = { session_id: 'source-session-1', source_id: sourceId, dataset_id: null, mode: 'AUTO', message_count: 0 }
const NO = { x: null, y: [], series: null, label: null, value: null }
const result = {
  execution_id: 'abcdef12-0000-0000-0000-000000000000', status: 'succeeded', source_id: sourceId, snapshot_id: 'snapshot-1',
  columns: [{ alias: 'n', logical_type: 'integer', expected_postgres_type: 'int8', actual_postgres_type: 'int8' }],
  rows: [[{ kind: 'integer', value: '42' }]], returned_row_count: 1, complete: true, duration_ms: 3, session_time_zone: 'UTC', warnings: [], ordering: [],
}
const presentation = { answer: 'There are 42 orders.', takeaways: [], caveats: [], display: 'kpi', encodings: { ...NO, value: 'n' }, table_columns: [], fallback: null, derived: {} }
const GOOD = 'SELECT count(*) AS n FROM sales.orders'

let transcript
function configure({ sessions = [], messages = [], aiSettings, freshIds = false } = {}) {
  transcript = messages
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}`) return Promise.resolve({ source_id: sourceId, display_name: 'Shop' })
    if (path === `/sources/${sourceId}/catalog`) return Promise.resolve({ source_id: sourceId, current_snapshot_id: 'snapshot-1' })
    if (path === `/sources/${sourceId}/chat/sessions?limit=50`) return Promise.resolve({ sessions, count: sessions.length })
    if (path.endsWith('/messages?limit=500')) return Promise.resolve({ session_id: path.split('/')[3], messages: transcript, count: transcript.length })
    if (path === `/sources/${sourceId}/ai-settings`) return aiSettings ? Promise.resolve(aiSettings) : Promise.reject(new Error('no'))
    throw new Error(`unexpected GET ${path}`)
  })
  let created = 0
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/chat/sessions`) return Promise.resolve({ session: { ...session, session_id: freshIds || created++ ? `new-session-${++created}` : 'source-session-1' } })
    throw new Error(`unexpected POST ${path}`)
  })
}
function renderChat() {
  return render(<MemoryRouter initialEntries={[`/sources/${sourceId}/chat`]}><Routes><Route path="/sources/:sourceId/chat" element={<DatabaseChat />} /></Routes></MemoryRouter>)
}
async function composer() {
  const box = await screen.findByRole('textbox', { name: 'Database question' })
  await waitFor(() => expect(box.disabled).toBe(false))
  return box
}
async function ask(question = 'How many orders are there?') {
  const box = await composer()
  fireEvent.change(box, { target: { value: question } })
  fireEvent.click(screen.getByRole('button', { name: 'Send database question' }))
}
const failWith = (init) => () => ({ [Symbol.asyncIterator]() { return this }, next: () => Promise.reject(Object.assign(new Error(init.message ?? 'fail'), init)), return: async () => ({ done: true }) })

beforeEach(() => {
  window.sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  configure()
})
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  vi.useRealTimers()
})

it.each([
  ['E9', { status: 409, response: { error: { code: 'turn_in_progress' } } }, /still being answered/],
  ['E10', { status: 429, response: { error: 'rate_limit_exceeded', retry_after: 600 } }, /about 10 minutes/],
  ['E6', { code: 'connect_timeout', transient: true }, /Your question wasn't sent — it's back in the box/],
  ['E8', { status: 401 }, /sign-in expired/],
])('%s: a pre-acceptance failure removes the bubbles, restores the question and explains', async (_id, init, text) => {
  api.streamSSE.mockImplementation(failWith(init))
  renderChat()
  await ask('How many orders?')
  expect((await screen.findByRole('alert')).textContent).toMatch(text)
  expect(screen.queryByText('How many orders?', { selector: '.chat-user' })).toBeNull()
  expect((await composer()).value).toBe('How many orders?')
  expect(api.streamSSE).toHaveBeenCalledTimes(1)                         // I5: never re-sent
})

it('keeps what the user typed meanwhile, and typing dismisses the notice (W6.7)', async () => {
  api.streamSSE.mockImplementation(failWith({ status: 429, response: { retry_after: 60 } }))
  renderChat()
  await ask('first')
  await screen.findByRole('alert')
  fireEvent.change(await composer(), { target: { value: 'second' } })
  expect(screen.queryByRole('alert')).toBeNull()
})

it('E9 offers Refresh conversation, which only reads the transcript (I6)', async () => {
  api.streamSSE.mockImplementation(failWith({ status: 409, response: { error: { code: 'turn_in_progress' } } }))
  renderChat()
  await ask()
  fireEvent.click(await screen.findByRole('button', { name: 'Refresh conversation' }))
  await waitFor(() => expect(api.get.mock.calls.filter(([p]) => p.endsWith('/messages?limit=500')).length).toBeGreaterThan(0))
  expect(api.post.mock.calls.every(([p]) => p.endsWith('/chat/sessions'))).toBe(true)
})

it('E11: after a drop, reconcile replaces the local view with the stored answer', async () => {
  const stored = [
    { message_id: 'u1', role: 'USER', content: 'How many orders are there?' },
    { message_id: 'a1', role: 'ASSISTANT', content: presentation.answer, postgres_result: result, nl_sql: { outcome: 'answer', sql: GOOD, assumptions: [], repair: null, presentation } },
  ]
  async function* stream() {
    yield { event: 'start', data: { turn_id: 't' } }
    transcript = stored
    throw Object.assign(new Error('net'), { transient: true })
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  expect((await screen.findByTestId('answer-text')).textContent).toBe('There are 42 orders.')
  expect(screen.queryByText('The connection was lost while answering.')).toBeNull()
})

it('shows each stage, including Writing the answer… for the presenting event (E16 handler)', async () => {
  const gates = []
  const wait = () => new Promise(resolve => gates.push(resolve))
  async function* stream() {
    yield { event: 'start', data: { turn_id: 't' } }
    await wait()
    yield { event: 'sql_generated', data: { sql: GOOD, assumptions: [], attempt: 1 } }
    yield { event: 'executing', data: { state: 'executing' } }
    await wait()
    yield { event: 'presenting', data: { state: 'presenting' } }
    await wait()
    yield { event: 'unknown_future_event', data: { x: 1 } }
    yield { event: 'postgres_result', data: { postgres_result: result } }
    yield { event: 'presentation', data: presentation }
    yield { event: 'token', data: { text: presentation.answer } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  await screen.findByText('Understanding the question…')
  gates[0]()
  await screen.findByText('Running the query on Shop…')
  gates[1]()
  await screen.findByText('Writing the answer…')
  gates[2]()
  expect((await screen.findByTestId('answer-text')).textContent).toBe('There are 42 orders.')
  expect(screen.queryByText('Writing the answer…')).toBeNull()
})

it('waits visibly for a cold server: Waking the server after 8 s', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  api.streamSSE.mockImplementation(() => ({ [Symbol.asyncIterator]() { return this }, next: () => new Promise(() => {}), return: async () => ({ done: true }) }))
  renderChat()
  await ask()
  await screen.findByText('Sending your question…')
  await act(async () => { await vi.advanceTimersByTimeAsync(9000) })
  expect(screen.getByText(/Waking the server/)).toBeTruthy()
})

it('a repair shows both attempts with the reason, and a failed repair keeps both (E17, E18)', async () => {
  async function* stream() {
    yield { event: 'sql_generated', data: { sql: 'SELECT cnt FROM sales.orders', assumptions: [], attempt: 1 } }
    yield { event: 'repairing', data: { reason_code: 'column_not_found' } }
    yield { event: 'sql_generated', data: { sql: GOOD, assumptions: [], attempt: 2 } }
    yield { event: 'executing', data: {} }
    yield { event: 'error', data: { code: 'query_error', message: "I couldn't write a working query for this question. Try rephrasing it." } }
    yield { event: 'token', data: { text: "I couldn't write a working query for this question. Try rephrasing it." } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  expect(await screen.findByText('Query that was tried')).toBeTruthy()
  expect(screen.getByRole('tab', { name: "First attempt (failed: a column name didn't match)" })).toBeTruthy()
  expect(screen.getByRole('tab', { name: 'Last query' })).toBeTruthy()
  expect(screen.getAllByText(/couldn't write a working query/)).toHaveLength(1)
})

it('a failed turn shows the backend text, Edit question and, for a retryable code, Try again as a new turn (E3, W5.6)', async () => {
  const text = 'This question took too long to answer. Try a narrower question, or try again in a moment.'
  let calls = 0
  async function* stream() {
    calls += 1
    yield { event: 'start', data: { turn_id: 't' } }
    yield { event: 'error', data: { code: 'turn_timeout', message: text } }
    yield { event: 'token', data: { text } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask('slow question')
  expect(await screen.findByText(text)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Edit question' }))
  expect((await composer()).value).toBe('slow question')
  expect(api.streamSSE).toHaveBeenCalledTimes(1)                         // editing never sends
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  await waitFor(() => expect(calls).toBe(2))                              // a user click = a new turn
})

it('does not offer Try again for a non-retryable failure', async () => {
  async function* stream() {
    yield { event: 'start', data: {} }
    yield { event: 'error', data: { code: 'query_error', message: 'Rephrase it.' } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  await screen.findByText('Rephrase it.')
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Edit question' })).toBeTruthy()
})

it('reloads a failed SQL turn with its SQL and a timed-out turn as a failed card (E41)', async () => {
  const stored = [
    { message_id: 'u', role: 'USER', content: 'q' },
    { message_id: 'a', role: 'ASSISTANT', content: 'Access to the data changed while the query ran. No rows are shown.', degraded: true, degraded_reason: 'access_revoked',
      nl_sql: { outcome: 'failed', failure_code: 'access_revoked', sql: GOOD, assumptions: [], repair: null, presentation: null } },
  ]
  configure({ sessions: [{ ...session, message_count: 2 }], messages: stored })
  renderChat()
  expect(await screen.findByText('Query that was tried')).toBeTruthy()
  expect(screen.getByText('Access to the data changed while the query ran. No rows are shown.')).toBeTruthy()
})

it('E19: a failure with no SQL has no SQL panel', async () => {
  async function* stream() {
    yield { event: 'start', data: {} }
    yield { event: 'error', data: { code: 'canary_leak', message: "I couldn't answer that safely. Please rephrase the question." } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  await screen.findByText(/couldn't answer that safely/)
  expect(screen.queryByText('Query that was tried')).toBeNull()
})

it('E20: a clarification focuses the composer once the turn ends', async () => {
  async function* stream() {
    yield { event: 'start', data: {} }
    yield { event: 'clarification', data: { message: 'Which year?', outcome: 'clarification' } }
    yield { event: 'token', data: { text: 'Which year?' } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  await screen.findByText('Which year?')
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Database question' })))
})

it('E23: a lost presentation frame still renders an answer from the saved text', async () => {
  async function* stream() {
    yield { event: 'sql_generated', data: { sql: GOOD, assumptions: [], attempt: 1 } }
    yield { event: 'postgres_result', data: { postgres_result: result } }
    yield { event: 'token', data: { text: 'The read-only calculation completed with 1 returned row(s).' } }
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  expect((await screen.findByTestId('answer-text')).textContent).toBe('The read-only calculation completed with 1 returned row(s).')
  expect(screen.getByText('Basic summary')).toBeTruthy()
})

it('E44: New conversation starts a fresh session and clears the list', async () => {
  const stored = [{ message_id: 'u', role: 'USER', content: 'old question' }, { message_id: 'a', role: 'ASSISTANT', content: 'old answer' }]
  configure({ sessions: [{ ...session, message_count: 2 }], messages: stored, freshIds: true })
  renderChat()
  await screen.findByText('old answer')
  fireEvent.click(screen.getByRole('button', { name: 'New conversation' }))
  await waitFor(() => expect(screen.queryByText('old answer')).toBeNull())
  expect(window.sessionStorage.getItem(`neuronova:source-chat-session:v1:${sourceId}`)).toMatch(/^new-session/)
  expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Database question' }))
})

it('E45: a failed New conversation shows an error and keeps the conversation', async () => {
  const stored = [{ message_id: 'u', role: 'USER', content: 'old question' }, { message_id: 'a', role: 'ASSISTANT', content: 'old answer' }]
  configure({ sessions: [{ ...session, message_count: 2 }], messages: stored })
  renderChat()
  await screen.findByText('old answer')
  api.post.mockRejectedValue(Object.assign(new Error('x'), { status: 500 }))
  fireEvent.click(screen.getByRole('button', { name: 'New conversation' }))
  expect((await screen.findByRole('alert')).textContent).toMatch(/current conversation is unchanged/)
  expect(screen.getByText('old answer')).toBeTruthy()
})

it('E46: a 4 001-character question cannot be sent', async () => {
  renderChat()
  fireEvent.change(await composer(), { target: { value: 'x'.repeat(4001) } })
  expect(screen.getByRole('button', { name: 'Send database question' }).disabled).toBe(true)
  expect(screen.getByText(/The question is too long/)).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox', { name: 'Database question' }), { target: { value: 'x'.repeat(3600) } })
  expect(screen.getByText(/3600\/4000/)).toBeTruthy()
  expect(api.streamSSE).not.toHaveBeenCalled()
})

it('E43: no yank while the reader is scrolled up; a Jump to latest pill brings them back', async () => {
  let release
  const gate = new Promise(resolve => { release = resolve })
  async function* stream() {
    yield { event: 'start', data: {} }
    await gate
    yield { event: 'done', data: {} }
  }
  api.streamSSE.mockImplementation(() => stream())
  renderChat()
  await ask()
  await screen.findByText('Understanding the question…')
  const list = document.querySelector('.chat-messages')
  Object.defineProperty(list, 'scrollHeight', { value: 2000, configurable: true })
  Object.defineProperty(list, 'clientHeight', { value: 400, configurable: true })
  list.scrollTop = 0
  fireEvent.scroll(list)
  const pill = await screen.findByRole('button', { name: 'Jump to latest' })
  expect(list.scrollTop).toBe(0)
  fireEvent.click(pill)
  expect(list.scrollTop).toBe(2000)
  release()
})

it('shows the AI settings read-only and hides the block when they cannot be read (W6.6)', async () => {
  configure({ aiSettings: { result_rows_to_model: true, sample_values_enabled: false } })
  const { unmount } = renderChat()
  expect(await screen.findByText('AI settings')).toBeTruthy()
  expect(screen.getByText('On')).toBeTruthy()
  expect(screen.getByText('Off')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Change' }).getAttribute('href')).toBe('/upload')
  unmount()
  configure()
  renderChat()
  await composer()
  expect(screen.queryByText('AI settings')).toBeNull()
})

it('E50: the copy no longer promises one table or no charts', async () => {
  renderChat()
  await composer()
  expect(screen.queryByText(/one authorized table/)).toBeNull()
  expect(screen.getByText(/Read-only questions across the tables this source shares/)).toBeTruthy()
  expect(screen.getByText('Read-only · answers show their SQL')).toBeTruthy()
  expect(within(document.body).queryByText(/Arbitrary joins are not available/)).toBeNull()
})

// ── Greptile review findings ────────────────────────────────────────────────

const okStream = () => (async function* () {
  yield { event: 'start', data: { turn_id: 't' } }
  yield { event: 'sql_generated', data: { sql: GOOD, assumptions: [], attempt: 1 } }
  yield { event: 'postgres_result', data: { postgres_result: result } }
  yield { event: 'presentation', data: presentation }
  yield { event: 'token', data: { text: presentation.answer } }
  yield { event: 'done', data: {} }
})()
const saved = (question, id) => [
  { message_id: `u${id}`, role: 'USER', content: question },
  { message_id: `a${id}`, role: 'ASSISTANT', content: presentation.answer, postgres_result: result, nl_sql: { outcome: 'answer', sql: GOOD, assumptions: [], repair: null, presentation } },
]

it('P1: an unsent question stays in the box after an earlier turn succeeded in this page', async () => {
  renderChat()
  api.streamSSE.mockImplementationOnce(() => okStream())
  await ask('first question')
  await screen.findByTestId('answer-text')
  transcript = saved('first question', 1)                               // the server holds only the first turn
  api.streamSSE.mockImplementationOnce(failWith({ code: 'connect_timeout', transient: true }))
  await ask('second question')
  expect((await screen.findByRole('alert')).textContent).toMatch(/back in the box/)
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Database question' }).value).toBe('second question'))
})

it('P1: asking the same question again does not let the earlier copy clear the box', async () => {
  renderChat()
  api.streamSSE.mockImplementationOnce(() => okStream())
  await ask('same question')
  await screen.findByTestId('answer-text')
  transcript = saved('same question', 1)
  api.streamSSE.mockImplementationOnce(failWith({ code: 'connect_timeout', transient: true }))
  await ask('same question')
  await screen.findByRole('alert')
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Database question' }).value).toBe('same question'))
})

it('P1: a transcript read that started before a new send cannot delete the new turn', async () => {
  renderChat()
  let releaseRead
  const gate = new Promise(resolve => { releaseRead = resolve })
  const realGet = api.get.getMockImplementation()
  api.streamSSE.mockImplementationOnce(() => (async function* () {
    yield { event: 'start', data: { turn_id: 't1' } }
    throw Object.assign(new Error('net'), { transient: true })
  })())
  api.get.mockImplementation(path => (path.endsWith('/messages?limit=500') ? gate.then(() => ({ messages: [] })) : realGet(path)))
  await ask('first')
  await screen.findByText('The connection was lost while answering.')   // reconcile is now waiting on the gate
  api.streamSSE.mockImplementationOnce(() => okStream())
  await ask('second')
  expect((await screen.findByTestId('answer-text')).textContent).toBe('There are 42 orders.')
  releaseRead()                                                          // the OLD, empty transcript arrives late
  await act(async () => { await Promise.resolve() })
  expect(screen.getByTestId('answer-text').textContent).toBe('There are 42 orders.')
  expect(screen.getByText('second', { selector: '.chat-user' })).toBeTruthy()
})

it('P1: sending is blocked while New conversation is being created, and creation cannot repeat', async () => {
  renderChat()
  await composer()
  let releaseCreate
  const gate = new Promise(resolve => { releaseCreate = resolve })
  const realPost = api.post.getMockImplementation()
  api.post.mockImplementation(path => (path === `/sources/${sourceId}/chat/sessions` ? gate.then(() => ({ session: { ...session, session_id: 'brand-new' } })) : realPost(path)))
  fireEvent.click(screen.getByRole('button', { name: 'New conversation' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Database question' }), { target: { value: 'typed while waiting' } })
  expect(screen.getByRole('button', { name: 'Send database question' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'New conversation' }).disabled).toBe(true)
  fireEvent.keyDown(screen.getByRole('textbox', { name: 'Database question' }), { key: 'Enter' })
  expect(api.streamSSE).not.toHaveBeenCalled()
  releaseCreate()
  await waitFor(() => expect(screen.getByRole('button', { name: 'New conversation' }).disabled).toBe(false))
  expect(api.post.mock.calls.filter(([path]) => path === `/sources/${sourceId}/chat/sessions`)).toHaveLength(2)   // load + one new
  expect(window.sessionStorage.getItem(`neuronova:source-chat-session:v1:${sourceId}`)).toBe('brand-new')
  expect(screen.getByRole('textbox', { name: 'Database question' }).value).toBe('typed while waiting')            // nothing lost
})
