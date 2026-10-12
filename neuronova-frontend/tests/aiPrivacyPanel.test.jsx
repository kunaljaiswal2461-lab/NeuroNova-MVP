// SPEC-09 W5.5: the AI and privacy settings panel.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import AiPrivacyPanel from '../src/components/sources/AiPrivacyPanel'

vi.mock('../src/api/client', () => ({
  api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

const sourceId = 'source-1'
const column = (table, name, suggested, state = null) => ({
  schema: 'sales', table, column: name, type: 'text', suggested_personal: suggested, state, effective_personal: state ? state === 'personal' : suggested,
})
const privacyRows = () => [
  column('customers', 'email', true),
  column('customers', 'segment', false),
  column('orders', 'note', true, 'not_personal'),
]
const samples = (extra = {}) => ({
  enabled: true, sample_values_generation: 1, excluded_columns: [], collected: [{ schema: 'sales', table: 'customers', column: 'segment', method: 'pg_stats', value_count: 4 }],
  skipped: [{ schema: 'sales', table: 'customers', column: 'email', reason: 'pii_name' }], ...extra,
})

function wire({ ai = { result_rows_to_model: false, sample_values_enabled: true, result_rows_to_model_generation: 0 }, rows = privacyRows(), sampleState = samples() } = {}) {
  api.get.mockImplementation(path => {
    if (path.endsWith('/ai-settings')) return Promise.resolve(ai)
    if (path.endsWith('/sample-values')) return Promise.resolve(sampleState)
    if (path.endsWith('/column-privacy')) return Promise.resolve(rows)
    throw new Error(`unexpected GET ${path}`)
  })
}

// The panel is a closed <details> that loads on first open.
function openPanel() {
  const view = render(<AiPrivacyPanel sourceId={sourceId} />)
  const details = view.container.querySelector('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  return view
}

beforeEach(() => wire())
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('loads the three settings objects through JWT-only calls and never shows a value', async () => {
  openPanel()
  expect(screen.getByText('Loading AI and privacy settings…')).toBeTruthy()
  await screen.findByRole('checkbox', { name: 'Send result rows to the AI model' })
  expect(api.get).toHaveBeenCalledWith(`/sources/${sourceId}/ai-settings`, expect.objectContaining({ requireJwt: true }))
  expect(api.get).toHaveBeenCalledWith(`/sources/${sourceId}/sample-values`, expect.objectContaining({ requireJwt: true }))
  expect(api.get).toHaveBeenCalledWith(`/sources/${sourceId}/column-privacy`, expect.objectContaining({ requireJwt: true }))
  expect(screen.getByText(/1 columns have stored example values; 1 were skipped/)).toBeTruthy()
  expect(screen.getByText(/replaced with placeholders first/)).toBeTruthy()
})

it('the result-rows consent starts off and a toggle sends exactly that one field', async () => {
  api.put.mockResolvedValue({ result_rows_to_model: true, sample_values_enabled: true, result_rows_to_model_generation: 1 })
  openPanel()
  const toggle = await screen.findByRole('checkbox', { name: 'Send result rows to the AI model' })
  expect(toggle.checked).toBe(false)
  fireEvent.click(toggle)
  await waitFor(() => expect(api.put).toHaveBeenCalledWith(`/sources/${sourceId}/ai-settings`, { result_rows_to_model: true }, expect.anything()))
  await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Send result rows to the AI model' }).checked).toBe(true))
  expect(screen.getByText('Result rows can now be sent to the AI model.')).toBeTruthy()
})

it('the example-values toggle uses the Part 4 endpoint', async () => {
  api.put.mockResolvedValue(samples({ enabled: false, collected: [], skipped: [] }))
  openPanel()
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Use example values in questions' }))
  await waitFor(() => expect(api.put).toHaveBeenCalledWith(`/sources/${sourceId}/sample-values`, { enabled: false }, expect.anything()))
})

it('lists text columns with their suggestion and sends only the rows that changed', async () => {
  api.put.mockResolvedValue([column('customers', 'email', true), column('customers', 'segment', false, 'personal'), column('orders', 'note', true, 'not_personal')])
  openPanel()
  await screen.findByRole('region', { name: 'Text columns' })
  const save = screen.getByRole('button', { name: /Save personal-data changes/ })
  expect(save.disabled).toBe(true)
  // the stored override is preselected; the suggestion otherwise
  expect(screen.getByLabelText('Personal data setting for sales.orders.note').value).toBe('not_personal')
  expect(screen.getByLabelText('Personal data setting for sales.customers.email').value).toBe('suggestion')
  fireEvent.change(screen.getByLabelText('Personal data setting for sales.customers.segment'), { target: { value: 'personal' } })
  expect(screen.getByRole('button', { name: 'Save personal-data changes (1)' }).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Save personal-data changes (1)' }))
  await waitFor(() => expect(api.put).toHaveBeenCalledWith(
    `/sources/${sourceId}/column-privacy`,
    { changes: [{ schema: 'sales', table: 'customers', column: 'segment', state: 'personal' }] },
    expect.anything(),
  ))
  await screen.findByText('Personal-data settings saved.')
})

it('"Use suggestion" clears an override with null', async () => {
  api.put.mockResolvedValue(privacyRows())
  openPanel()
  fireEvent.change(await screen.findByLabelText('Personal data setting for sales.orders.note'), { target: { value: 'suggestion' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save personal-data changes (1)' }))
  await waitFor(() => expect(api.put).toHaveBeenCalledWith(
    `/sources/${sourceId}/column-privacy`,
    { changes: [{ schema: 'sales', table: 'orders', column: 'note', state: null }] },
    expect.anything(),
  ))
})

it('changing a row back to what it was sends nothing', async () => {
  openPanel()
  const select = await screen.findByLabelText('Personal data setting for sales.customers.email')
  fireEvent.change(select, { target: { value: 'personal' } })
  fireEvent.change(select, { target: { value: 'suggestion' } })
  expect(screen.getByRole('button', { name: /Save personal-data changes/ }).disabled).toBe(true)
})

it('saves the excluded-columns list through the sample-values endpoint, keeping unlisted exclusions', async () => {
  wire({ sampleState: samples({ excluded_columns: [{ schema: 'sales', table: 'gone', column: 'old' }] }) })
  api.put.mockResolvedValue(samples())
  openPanel()
  fireEvent.click(await screen.findByLabelText('Exclude example values of sales.customers.segment'))
  fireEvent.click(screen.getByRole('button', { name: 'Save excluded columns' }))
  await waitFor(() => expect(api.put).toHaveBeenCalled())
  const [path, body] = api.put.mock.calls[0]
  expect(path).toBe(`/sources/${sourceId}/sample-values`)
  expect(body.excluded_columns).toEqual(expect.arrayContaining([
    { schema: 'sales', table: 'customers', column: 'segment' },
    { schema: 'sales', table: 'gone', column: 'old' },
  ]))
  expect(body.excluded_columns).toHaveLength(2)
})

it('search narrows the column list', async () => {
  openPanel()
  const region = await screen.findByRole('region', { name: 'Text columns' })
  expect(within(region).getAllByRole('row')).toHaveLength(4)
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'note' } })
  expect(within(region).getAllByRole('row')).toHaveLength(2)
})

it('stays silent when the source has vanished (the inspector already says so)', async () => {
  api.get.mockRejectedValue(Object.assign(new Error('gone'), { response: { error: { code: 'source_not_found' } } }))
  const { container } = openPanel()
  await waitFor(() => expect(container.firstChild).toBeNull())
})

it('shows a safe error when loading fails, and retries', async () => {
  api.get.mockRejectedValueOnce(Object.assign(new Error('boom'), { response: { error: { code: 'column_privacy_persistence_failed' } } }))
  api.get.mockImplementation(path => {
    if (path.endsWith('/ai-settings')) return Promise.resolve({ result_rows_to_model: false, sample_values_enabled: true, result_rows_to_model_generation: 0 })
    if (path.endsWith('/sample-values')) return Promise.resolve(samples())
    return Promise.resolve([])
  })
  openPanel()
  expect((await screen.findByRole('alert')).textContent).toBe('The change could not be saved. Nothing was changed.')
  api.get.mockClear()
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  await screen.findByRole('checkbox', { name: 'Send result rows to the AI model' })
})

it('shows a safe error when a save fails and leaves the setting as it was', async () => {
  api.put.mockRejectedValue(Object.assign(new Error('x'), { response: { error: { code: 'column_privacy_persistence_failed', message: 'SQL error: secret detail' } } }))
  openPanel()
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Send result rows to the AI model' }))
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).not.toContain('secret detail')
  expect(screen.getByRole('checkbox', { name: 'Send result rows to the AI model' }).checked).toBe(false)
})

it('requests nothing until it is opened', () => {
  render(<AiPrivacyPanel sourceId={sourceId} />)
  expect(api.get).not.toHaveBeenCalled()
  expect(screen.getByText('AI and privacy')).toBeTruthy()
})
