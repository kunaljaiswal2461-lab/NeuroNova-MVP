import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { DatasetProvider } from '../src/context/DatasetContext'
import Upload from '../src/pages/Upload'

vi.mock('../src/api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
    postForm: vi.fn(),
  },
}))

const eligibleTables = [
  { schema_name: 'public', table_name: 'orders' },
  { schema_name: 'public', table_name: 'customers' },
  { schema_name: 'reporting', table_name: 'daily_sales' },
]

const sourceSummary = {
  source_id: 'source-1',
  display_name: 'Production analytics',
  source_type: 'POSTGRESQL',
  status: 'TESTED',
  host: 'db.example.com',
  port: 5432,
  database_name: 'analytics',
  tls_mode: 'VERIFY_FULL',
  has_custom_ca: false,
  profiling_enabled: false,
  scope_count: 1,
  last_tested_at: '2026-10-03T12:00:00Z',
  created_at: '2026-10-03T12:00:00Z',
  updated_at: '2026-10-03T12:00:00Z',
}

function renderUpload() {
  return render(
    <MemoryRouter initialEntries={['/upload']}>
      <DatasetProvider><Upload /></DatasetProvider>
    </MemoryRouter>,
  )
}

async function openDatabase() {
  fireEvent.click(screen.getByRole('tab', { name: 'Database' }))
  await waitFor(() => expect(api.get).toHaveBeenCalledWith('/sources', expect.objectContaining({
    requireJwt: true,
    preserveErrorBody: true,
  })))
}

function fillConnection(overrides = {}) {
  const values = {
    displayName: 'Production analytics',
    host: 'db.example.com',
    databaseName: 'analytics',
    username: 'reader',
    password: 'secret',
    ...overrides,
  }
  fireEvent.change(screen.getByLabelText('Connection name'), { target: { value: values.displayName } })
  fireEvent.change(screen.getByLabelText('Public DNS hostname'), { target: { value: values.host } })
  fireEvent.change(screen.getByLabelText('Database name'), { target: { value: values.databaseName } })
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: values.username } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: values.password } })
}

async function testConnection(tables = eligibleTables) {
  fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  await screen.findByRole('heading', { name: 'Eligible tables' })
  expect(api.post).toHaveBeenCalledWith('/sources/postgresql/test', {
    host: 'db.example.com',
    port: 5432,
    database_name: 'analytics',
    tls: { mode: 'verify-full' },
    credentials: { username: 'reader', password: 'secret' },
  }, expect.objectContaining({ requireJwt: true, preserveErrorBody: true }))
  expect(screen.getByText('0 selected')).toBeTruthy()
  return tables
}

beforeEach(() => {
  localStorage.clear()
  api.get.mockImplementation(path => Promise.resolve(path === '/datasets' ? { items: [] } : { items: [] }))
  api.post.mockImplementation(path => {
    if (path === '/sources/postgresql/test') {
      return Promise.resolve({ status: 'reachable', eligible_tables: eligibleTables, count: eligibleTables.length })
    }
    return Promise.resolve({ ...sourceSummary, selected_tables: [eligibleTables[0]] })
  })
  api.delete.mockResolvedValue(null)
  api.postForm.mockResolvedValue({ dataset_id: 'file-1', status: 'QUEUED' })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.resetAllMocks()
})

it('uses accessible tabs without unmounting an active file upload', async () => {
  let acknowledge
  api.postForm.mockImplementation(() => new Promise(resolve => { acknowledge = resolve }))
  renderUpload()
  const file = new File(['csv'], 'active.csv', { type: 'text/csv' })
  fireEvent.change(screen.getByLabelText('Upload dataset'), { target: { files: [file] } })
  const uploadSignal = api.postForm.mock.calls[0][2].signal

  fireEvent.keyDown(screen.getByRole('tab', { name: 'File' }), { key: 'ArrowRight' })
  expect(screen.getByRole('tab', { name: 'Database' }).getAttribute('aria-selected')).toBe('true')
  expect(uploadSignal.aborted).toBe(false)
  expect(document.getElementById('file-source-panel').hidden).toBe(true)

  await act(async () => acknowledge({ dataset_id: 'file-1', status: 'QUEUED' }))
  expect(uploadSignal.aborted).toBe(false)
})

it('uses default-deny table scope with partial schemas, filtering, select-all, and exclusions', async () => {
  await renderUpload()
  await openDatabase()
  fillConnection()
  await testConnection()

  const publicSchema = screen.getByRole('checkbox', { name: 'public schema selection' })
  const orders = screen.getByRole('checkbox', { name: '"public"."orders"' })
  const customers = screen.getByRole('checkbox', { name: '"public"."customers"' })
  expect(orders.checked).toBe(false)
  expect(publicSchema.checked).toBe(false)

  fireEvent.click(orders)
  expect(publicSchema.indeterminate).toBe(true)
  expect(screen.getByText('1 selected')).toBeTruthy()

  fireEvent.click(publicSchema)
  expect(publicSchema.checked).toBe(true)
  expect(customers.checked).toBe(true)
  expect(screen.getByText('2 selected')).toBeTruthy()
  fireEvent.click(customers)
  expect(publicSchema.indeterminate).toBe(true)
  expect(screen.getByText('1 selected')).toBeTruthy()

  fireEvent.change(screen.getByPlaceholderText('Filter schemas or tables'), { target: { value: 'customers' } })
  expect(screen.queryByRole('checkbox', { name: '"public"."orders"' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /Select all current tables/ }))
  expect(screen.getByText('3 selected')).toBeTruthy()

  fireEvent.change(screen.getByPlaceholderText('Filter schemas or tables'), { target: { value: '' } })
  expect(orders.checked).toBe(true)
  fireEvent.click(customers)
  expect(publicSchema.indeterminate).toBe(true)
  expect(screen.getByText('2 selected')).toBeTruthy()
})

it('invalidates inventory and ignores a stale test response after connection settings change', async () => {
  let resolveTest
  api.post.mockImplementation(path => {
    if (path === '/sources/postgresql/test') return new Promise(resolve => { resolveTest = resolve })
    return Promise.resolve({ ...sourceSummary, selected_tables: [] })
  })
  renderUpload()
  await openDatabase()
  fillConnection()
  fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/sources/postgresql/test', expect.anything(), expect.anything()))

  fireEvent.change(screen.getByLabelText('Public DNS hostname'), { target: { value: 'new-db.example.com' } })
  await act(async () => resolveTest({ status: 'reachable', eligible_tables: eligibleTables, count: eligibleTables.length }))
  expect(screen.queryByRole('heading', { name: 'Eligible tables' })).toBeNull()

  api.post.mockImplementation(path => path === '/sources/postgresql/test'
    ? Promise.resolve({ status: 'reachable', eligible_tables: eligibleTables, count: eligibleTables.length })
    : Promise.resolve({ ...sourceSummary, selected_tables: [] }))
  fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  await screen.findByRole('heading', { name: 'Eligible tables' })
  fireEvent.click(screen.getByRole('checkbox', { name: '"public"."orders"' }))
  expect(screen.getByText('1 selected')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'changed-secret' } })
  expect(screen.queryByRole('heading', { name: 'Eligible tables' })).toBeNull()
})

it('saves exact selected scope once, clears transient credentials, and refreshes the saved list', async () => {
  const unusualTable = { schema_name: 'billing.prod', table_name: 'orders:2026' }
  const created = {
    ...sourceSummary,
    source_id: 'source-created',
    selected_tables: [unusualTable],
    scope_count: 1,
  }
  let resolveSave
  let saved = false
  api.get.mockImplementation(path => {
    if (path === '/datasets') return Promise.resolve({ items: [] })
    return Promise.resolve({ items: saved ? [created] : [] })
  })
  api.post.mockImplementation(path => {
    if (path === '/sources/postgresql/test') {
      return Promise.resolve({ status: 'reachable', eligible_tables: [unusualTable], count: 1 })
    }
    return new Promise(resolve => { resolveSave = resolve })
  })
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  renderUpload()
  await openDatabase()
  fillConnection()
  await testConnection([unusualTable])
  fireEvent.click(screen.getByRole('checkbox', { name: '"billing.prod"."orders:2026"' }))
  const save = screen.getByRole('button', { name: 'Save selected connection' })
  fireEvent.click(save)
  fireEvent.click(save)
  expect(api.post.mock.calls.filter(([path]) => path === '/sources')).toHaveLength(1)
  expect(screen.getByRole('checkbox', { name: 'billing.prod schema selection' }).disabled).toBe(true)
  expect(screen.getByPlaceholderText('Filter schemas or tables').disabled).toBe(true)
  expect(api.post).toHaveBeenLastCalledWith('/sources', {
    host: 'db.example.com',
    port: 5432,
    database_name: 'analytics',
    tls: { mode: 'verify-full' },
    credentials: { username: 'reader', password: 'secret' },
    display_name: 'Production analytics',
    selected_tables: [unusualTable],
    profiling_enabled: false,
  }, expect.objectContaining({ requireJwt: true, preserveErrorBody: true }))

  saved = true
  await act(async () => resolveSave(created))
  await screen.findByText('Connection saved. Metadata synchronization and database querying will be available in later stages.')
  expect(screen.getByLabelText('Username').value).toBe('')
  expect(screen.getByLabelText('Password').value).toBe('')
  expect(screen.getByText('Previously tested')).toBeTruthy()
  expect(setItem).not.toHaveBeenCalled()
})

it('renders quoted schema and table identities without dot collisions', async () => {
  const ambiguousScopes = [
    { schema_name: 'a.b', table_name: 'c' },
    { schema_name: 'a', table_name: 'b.c' },
  ]
  const savedDetail = { ...sourceSummary, selected_tables: ambiguousScopes, scope_count: 2 }
  api.get.mockImplementation(path => {
    if (path === '/datasets') return Promise.resolve({ items: [] })
    if (path === '/sources/source-1') return Promise.resolve(savedDetail)
    return Promise.resolve({ items: [{ ...sourceSummary, scope_count: 2 }] })
  })

  renderUpload()
  await openDatabase()
  await screen.findByText('Production analytics')
  fireEvent.click(screen.getByRole('button', { name: 'View selected tables' }))

  await screen.findByText('"a.b"."c"')
  expect(screen.getByText('"a"."b.c"')).toBeTruthy()
})

it('does not render an unexpected source status as a catalog-readiness claim', async () => {
  api.get.mockImplementation(path => {
    if (path === '/datasets') return Promise.resolve({ items: [] })
    return Promise.resolve({ items: [{ ...sourceSummary, status: 'CATALOG_READY' }] })
  })

  renderUpload()
  await openDatabase()
  await screen.findByText('Production analytics')
  expect(screen.getByText('Status unavailable')).toBeTruthy()
  expect(screen.queryByText('CATALOG_READY')).toBeNull()
})

it('requires a fresh test when the backend says the selected scope changed during save', async () => {
  api.post.mockImplementation(path => {
    if (path === '/sources/postgresql/test') {
      return Promise.resolve({ status: 'reachable', eligible_tables: eligibleTables, count: eligibleTables.length })
    }
    return Promise.reject({ response: { error: { code: 'invalid_source_scope' } } })
  })
  renderUpload()
  await openDatabase()
  fillConnection()
  await testConnection()
  fireEvent.click(screen.getByRole('checkbox', { name: '"public"."orders"' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save selected connection' }))
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toContain('eligible table scope changed')
  expect(screen.queryByRole('heading', { name: 'Eligible tables' })).toBeNull()
})

it('clears transient connection values when the onboarding flow is cancelled', async () => {
  renderUpload()
  await openDatabase()
  fillConnection()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Use a custom certificate authority (optional)' }))
  fireEvent.change(screen.getByLabelText('Custom CA PEM'), { target: { value: 'temporary certificate' } })
  fireEvent.click(screen.getByRole('button', { name: 'Clear connection details' }))
  expect(screen.getByLabelText('Public DNS hostname').value).toBe('')
  expect(screen.getByLabelText('Username').value).toBe('')
  expect(screen.getByLabelText('Password').value).toBe('')
  expect(screen.queryByLabelText('Custom CA PEM')).toBeNull()
})

it('shows safe feature-disabled feedback while the File tab remains usable', async () => {
  api.post.mockRejectedValue({ response: { error: { code: 'connectors_disabled', message: 'operator-only detail' } } })
  renderUpload()
  await openDatabase()
  fillConnection()
  fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toContain('Database connections are currently unavailable')
  expect(screen.getByRole('alert').textContent).not.toContain('operator-only detail')

  fireEvent.click(screen.getByRole('tab', { name: 'File' }))
  const file = new File(['csv'], 'still-works.csv', { type: 'text/csv' })
  fireEvent.change(screen.getByLabelText('Upload dataset'), { target: { files: [file] } })
  expect(api.postForm).toHaveBeenCalledTimes(1)
})

it('lists selected scopes and allows deletion recovery without suggesting client data is deleted', async () => {
  const savedDetail = { ...sourceSummary, selected_tables: [{ schema_name: 'public', table_name: 'orders' }] }
  let listAfterDeletion = false
  api.get.mockImplementation(path => {
    if (path === '/datasets') return Promise.resolve({ items: [] })
    if (path === '/sources/source-1') return Promise.resolve(savedDetail)
    return Promise.resolve({ items: listAfterDeletion ? [] : [sourceSummary] })
  })
  api.delete.mockRejectedValueOnce({ response: { error: { code: 'connection_failed' } } }).mockResolvedValueOnce(null)
  renderUpload()
  await openDatabase()
  await screen.findByText('Production analytics')
  fireEvent.click(screen.getByRole('button', { name: 'View selected tables' }))
  await screen.findByText('"public"."orders"')

  fireEvent.click(screen.getByRole('button', { name: 'Delete connection' }))
  expect(screen.getByRole('alertdialog').textContent).toContain('does not delete tables or records from the client database')
  fireEvent.click(screen.getByRole('alertdialog').querySelector('button:last-child'))
  await screen.findByRole('alert')
  expect(api.delete).toHaveBeenCalledWith('/sources/source-1', undefined, expect.objectContaining({ requireJwt: true }))

  listAfterDeletion = true
  fireEvent.click(screen.getByRole('alertdialog').querySelector('button:last-child'))
  await waitFor(() => expect(screen.queryByText('Production analytics')).toBeNull())
  expect(api.delete).toHaveBeenCalledTimes(2)
})

it.each([
  ['connector_egress_not_configured', 'require administrator setup'],
  ['credential_key_unavailable', 'require administrator setup'],
  ['unsupported_connection_settings', 'Use a public DNS hostname'],
  ['connection_failed', 'We could not connect'],
  ['connection_timeout', 'did not respond in time'],
  ['tls_verification_failed', 'TLS certificate could not be verified'],
  ['inventory_limit_exceeded', 'more than 1,000 eligible tables'],
  ['no_eligible_tables', 'No eligible base tables'],
  ['invalid_source_scope', 'eligible table scope changed'],
])('maps the %s backend code to a safe onboarding message', async (code, expected) => {
  api.post.mockRejectedValue({ response: { error: { code, message: 'untrusted backend detail' } } })
  renderUpload()
  await openDatabase()
  fillConnection()
  fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toContain(expected)
  expect(alert.textContent).not.toContain('untrusted backend detail')
})
