import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import CatalogInspector from '../src/components/sources/CatalogInspector'
import { api } from '../src/api/client'

vi.mock('../src/api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

const source = {
  source_id: 'source-1',
  display_name: 'Production analytics',
}

const emptyCatalog = {
  source_id: 'source-1',
  latest_run: null,
  current_snapshot_id: null,
  current_snapshot_published_at: null,
  older_snapshot_available: false,
}

const publishedCatalog = {
  source_id: 'source-1',
  latest_run: {
    run_id: 'run-complete',
    state: 'SUCCEEDED',
    requested_at: '2026-10-03T12:00:00Z',
    started_at: '2026-10-03T12:00:01Z',
    completed_at: '2026-10-03T12:01:00Z',
    attempt_count: 1,
    next_attempt_at: null,
    failure_code: null,
  },
  current_snapshot_id: 'snapshot-1',
  current_snapshot_published_at: '2026-10-03T12:01:00Z',
  older_snapshot_available: false,
}

const assetPage = {
  source_id: 'source-1',
  snapshot_id: 'snapshot-1',
  published_at: '2026-10-03T12:01:00Z',
  page: 1,
  page_size: 50,
  total: 1,
  items: [{
    asset_id: 'asset-orders',
    schema_name: 'sales',
    table_name: 'orders',
    table_comment: '<img src=x onerror=alert(1)> customer orders',
  }],
}

const assetDetail = {
  source_id: 'source-1',
  snapshot_id: 'snapshot-1',
  published_at: '2026-10-03T12:01:00Z',
  scope_matches_current_selection: true,
  asset: assetPage.items[0],
  columns: [
    {
      column_name: 'tenant_id',
      ordinal_position: 1,
      is_nullable: false,
      formatted_native_type: 'uuid',
      column_comment: 'Tenant identifier',
    },
    {
      column_name: 'amount',
      ordinal_position: 2,
      is_nullable: true,
      formatted_native_type: 'numeric(12,2)',
      column_comment: '<script>not executable</script>',
    },
  ],
  declared_keys: [{
    constraint_name: 'orders_pkey',
    key_kind: 'PRIMARY_KEY',
    column_names: ['tenant_id', 'amount'],
  }],
  foreign_keys: [
    {
      constraint_name: 'orders_customer_fkey',
      target_status: 'IN_SCOPE',
      referenced_asset: { asset_id: 'asset-customers', schema_name: 'sales', table_name: 'customers' },
      column_mappings: [
        { ordinal_position: 1, referencing_column_name: 'tenant_id', referenced_column_name: 'tenant_id' },
        { ordinal_position: 2, referencing_column_name: 'customer_id', referenced_column_name: 'customer_id' },
      ],
    },
    {
      constraint_name: 'orders_private_fkey',
      target_status: 'TARGET_OUT_OF_SCOPE',
      referenced_asset: null,
      column_mappings: [],
    },
  ],
}

function jwtOptions() {
  return expect.objectContaining({ requireJwt: true, preserveErrorBody: true })
}

function renderInspector(overrides = {}) {
  return render(<CatalogInspector source={{ ...source, ...overrides }} onClose={vi.fn()} />)
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  api.get.mockResolvedValue(emptyCatalog)
  api.post.mockResolvedValue({
    run_id: 'run-queued',
    state: 'QUEUED',
    requested_at: '2026-10-03T12:00:00Z',
    started_at: null,
    completed_at: null,
    attempt_count: 0,
    next_attempt_at: null,
    failure_code: null,
  })
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  vi.restoreAllMocks()
  vi.resetAllMocks()
})

it('ignores a stale table-list response after the user switches sources', async () => {
  const oldAssets = deferred()
  const secondSource = { source_id: 'source-2', display_name: 'Warehouse analytics' }
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(publishedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) return oldAssets.promise
    if (path === '/sources/source-2/catalog') return Promise.resolve({ ...emptyCatalog, source_id: 'source-2' })
    return Promise.resolve({ ...emptyCatalog, source_id: 'source-2' })
  })
  const view = render(<CatalogInspector key={source.source_id} source={source} onClose={vi.fn()} />)

  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/sources/source-1/catalog/assets?snapshot_id=snapshot-1&page=1&page_size=50',
    jwtOptions(),
  ))
  view.rerender(<CatalogInspector key={secondSource.source_id} source={secondSource} onClose={vi.fn()} />)
  await screen.findByRole('heading', { name: 'Metadata: Warehouse analytics' })

  await act(async () => oldAssets.resolve(assetPage))
  expect(screen.queryByRole('button', { name: /"sales"\."orders"/ })).toBeNull()
  expect(screen.getByRole('heading', { name: 'Metadata not synchronized' })).toBeTruthy()
})

it('shows an unsynchronized source and requests a JWT-only metadata refresh', async () => {
  renderInspector()

  await screen.findByRole('heading', { name: 'Metadata not synchronized' })
  expect(api.get).toHaveBeenCalledWith('/sources/source-1/catalog', jwtOptions())

  fireEvent.click(screen.getByRole('button', { name: 'Refresh metadata' }))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/sources/source-1/catalog-syncs',
    {},
    jwtOptions(),
  ))
  await screen.findByText('Queued')
  expect(screen.getByText(/queued until a worker claims it/i)).toBeTruthy()
})

it('only enables the database-chat entry after a current snapshot is published', async () => {
  const first = renderInspector()
  const unavailable = await screen.findByRole('button', { name: 'Chat with this database' })
  expect(unavailable.disabled).toBe(true)
  first.unmount()

  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(publishedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) return Promise.resolve(assetPage)
    return Promise.resolve(emptyCatalog)
  })
  renderInspector()

  const entry = await screen.findByRole('link', { name: 'Chat with this database' })
  expect(entry.getAttribute('href')).toBe('/sources/source-1/chat')
})

it('pins list and detail reads to one published snapshot and renders composite metadata safely', async () => {
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(publishedCatalog)
    if (path === '/sources/source-1/catalog/assets?snapshot_id=snapshot-1&page=1&page_size=50') return Promise.resolve(assetPage)
    if (path === '/sources/source-1/catalog/assets/asset-orders?snapshot_id=snapshot-1') return Promise.resolve(assetDetail)
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
  renderInspector()

  const assetButton = await screen.findByRole('button', { name: /"sales"\."orders"/ })
  expect(api.get).toHaveBeenCalledWith(
    '/sources/source-1/catalog/assets?snapshot_id=snapshot-1&page=1&page_size=50',
    jwtOptions(),
  )
  fireEvent.click(assetButton)

  await screen.findByRole('heading', { name: '"sales"."orders"' })
  expect(api.get).toHaveBeenCalledWith(
    '/sources/source-1/catalog/assets/asset-orders?snapshot_id=snapshot-1',
    jwtOptions(),
  )
  expect(screen.getByText('numeric(12,2)')).toBeTruthy()
  expect(screen.getByText('orders_pkey')).toBeTruthy()
  expect(screen.getByText(/References "sales"\."customers"/)).toBeTruthy()
  expect(screen.getByText(/Target relationship omitted because the referenced table is outside/i)).toBeTruthy()
  expect(screen.getByText('"tenant_id" → "tenant_id"')).toBeTruthy()
  expect(screen.getByText('"customer_id" → "customer_id"')).toBeTruthy()
  expect(screen.getAllByText('<img src=x onerror=alert(1)> customer orders').length).toBeGreaterThan(0)
  expect(screen.getByText('<script>not executable</script>')).toBeTruthy()
  expect(document.querySelector('img')).toBeNull()
  expect(document.querySelector('script')).toBeNull()
})

it('does not combine a mismatched list response with the displayed snapshot', async () => {
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(publishedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) {
      return Promise.resolve({ ...assetPage, snapshot_id: 'different-snapshot' })
    }
    return Promise.resolve(emptyCatalog)
  })
  renderInspector()

  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toContain('Metadata changed while the table list was loading')
  expect(screen.queryByRole('button', { name: /"sales"\."orders"/ })).toBeNull()
})

it('explains that a historical snapshot is filtered when the current selected scope changed', async () => {
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(publishedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) {
      return Promise.resolve({ ...assetPage, scope_matches_current_selection: false })
    }
    return Promise.resolve(emptyCatalog)
  })
  renderInspector()

  await screen.findByRole('button', { name: /"sales"\."orders"/ })
  expect(screen.getByText(/selected table scope has changed since this snapshot/i)).toBeTruthy()
  expect(screen.getByText(/absent table does not prove it was dropped/i)).toBeTruthy()
})

it('shows a bounded comparison state without guessing table renames', async () => {
  const diff = {
    source_id: 'source-1',
    snapshot_id: 'snapshot-1',
    state: 'compared',
    previous_snapshot_id: 'snapshot-0',
    truncated: false,
    change_limit: 200,
    tables_added: [{ schema_name: 'sales', table_name: 'orders_v2' }],
    tables_removed: [{ schema_name: 'sales', table_name: 'orders' }],
    columns_added: [],
    columns_removed: [],
    columns_changed: [{
      schema_name: 'sales', table_name: 'orders_v2', column_name: 'amount',
      previous_formatted_native_type: 'integer', current_formatted_native_type: 'numeric(12,2)',
      previous_is_nullable: false, current_is_nullable: true,
    }],
    keys_changed: [{
      change: 'added', schema_name: 'sales', table_name: 'orders_v2', constraint_name: 'orders_v2_pkey',
      key_kind: 'PRIMARY_KEY', column_names: ['tenant_id', 'amount'],
    }],
    foreign_keys_changed: [{
      change: 'removed', schema_name: 'sales', table_name: 'orders', constraint_name: 'private_fkey',
      target_status: 'TARGET_OUT_OF_SCOPE', referenced_asset: null, column_mappings: [],
    }],
    comments_changed: [{
      subject: 'column', schema_name: 'sales', table_name: 'orders_v2', column_name: 'amount',
      previous_comment: 'old', current_comment: 'new',
    }],
  }
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(publishedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) return Promise.resolve(assetPage)
    if (path === '/sources/source-1/catalog/diff?snapshot_id=snapshot-1') return Promise.resolve(diff)
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
  renderInspector()
  await screen.findByRole('button', { name: /"sales"\."orders"/ })

  fireEvent.click(screen.getByRole('button', { name: 'View structural changes' }))
  await screen.findByRole('heading', { name: 'Structural changes' })
  expect(api.get).toHaveBeenCalledWith('/sources/source-1/catalog/diff?snapshot_id=snapshot-1', jwtOptions())
  expect(screen.getByRole('heading', { name: 'Tables added' })).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Tables removed' })).toBeTruthy()
  expect(screen.getByText('"sales"."orders_v2"')).toBeTruthy()
  expect(screen.getAllByText('"sales"."orders"').length).toBeGreaterThan(0)
  expect(screen.getByText(/"sales"\."orders_v2"\."amount": integer → numeric\(12,2\); not nullable → nullable/)).toBeTruthy()
  expect(screen.getByText(/Added PRIMARY_KEY orders_v2_pkey on "sales"\."orders_v2"/)).toBeTruthy()
  expect(screen.getByText(/Removed foreign key private_fkey on "sales"\."orders" · target omitted because it is outside the authorized scope/)).toBeTruthy()
  expect(screen.getByText(/Database comment changed for "sales"\."orders_v2"\."amount"/)).toBeTruthy()
  expect(screen.queryByText(/renamed/i)).toBeNull()
})

it.each([
  ['baseline', 'This is the first published metadata snapshot'],
  ['scope_changed', 'selected table scope changed between snapshots'],
])('renders the %s comparison state without exposing historical details', async (state, expected) => {
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(publishedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) return Promise.resolve(assetPage)
    if (path === '/sources/source-1/catalog/diff?snapshot_id=snapshot-1') {
      return Promise.resolve({
        source_id: 'source-1', snapshot_id: 'snapshot-1', state,
        previous_snapshot_id: null, previous_snapshot_published_at: null,
        truncated: false, change_limit: 200,
        tables_added: [], tables_removed: [], columns_added: [], columns_removed: [],
        columns_changed: [], keys_changed: [], foreign_keys_changed: [], comments_changed: [],
      })
    }
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
  renderInspector()
  await screen.findByRole('button', { name: /"sales"\."orders"/ })
  fireEvent.click(screen.getByRole('button', { name: 'View structural changes' }))
  expect(await screen.findByText(new RegExp(expected, 'i'))).toBeTruthy()
  expect(screen.queryByRole('heading', { name: 'Tables added' })).toBeNull()
})

it('keeps a published snapshot visible while a refresh run is queued and supports cancellation', async () => {
  const queuedCatalog = {
    ...publishedCatalog,
    latest_run: {
      run_id: 'run-queued',
      state: 'QUEUED',
      requested_at: '2026-10-03T12:02:00Z',
      started_at: null,
      completed_at: null,
      attempt_count: 0,
      next_attempt_at: null,
      failure_code: null,
    },
  }
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(queuedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) return Promise.resolve(assetPage)
    return Promise.resolve(queuedCatalog.latest_run)
  })
  api.post.mockResolvedValue({ ...queuedCatalog.latest_run, state: 'CANCELLED', completed_at: '2026-10-03T12:03:00Z' })
  renderInspector()

  await screen.findByRole('button', { name: /"sales"\."orders"/ })
  expect(screen.getByText(/last published snapshot remains available/i)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel refresh' }))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/sources/source-1/catalog-syncs/run-queued/cancel',
    {},
    jwtOptions(),
  ))
})

it.each([
  ['QUEUED', 'Queued', /queued\. This does not confirm that a worker/i],
  ['RUNNING', 'Synchronizing', /Metadata is being read from the selected database tables/i],
  ['SUCCEEDED', 'Synchronized', /latest metadata refresh completed successfully/i],
  ['FAILED', 'Refresh failed', /latest metadata refresh did not complete/i],
  ['CANCELLED', 'Cancelled', /metadata refresh was cancelled/i],
])('renders the %s synchronization state without hiding an existing published snapshot', async (state, label, message) => {
  const catalog = {
    ...publishedCatalog,
    latest_run: {
      ...publishedCatalog.latest_run,
      run_id: `run-${state.toLowerCase()}`,
      state,
    },
  }
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(catalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) return Promise.resolve(assetPage)
    return Promise.resolve(catalog.latest_run)
  })
  renderInspector()

  await screen.findByRole('button', { name: /"sales"\."orders"/ })
  expect(screen.getByText(label)).toBeTruthy()
  expect(screen.getByText(message)).toBeTruthy()
  if (state === 'QUEUED' || state === 'RUNNING') {
    expect(screen.getByRole('button', { name: 'Cancel refresh' })).toBeTruthy()
  } else {
    expect(screen.queryByRole('button', { name: 'Cancel refresh' })).toBeNull()
  }
})

it('prevents duplicate refresh clicks while a first request is unsettled', async () => {
  const pendingRequest = deferred()
  api.post.mockReturnValue(pendingRequest.promise)
  renderInspector()
  await screen.findByRole('heading', { name: 'Metadata not synchronized' })

  const refresh = screen.getByRole('button', { name: 'Refresh metadata' })
  fireEvent.click(refresh)
  fireEvent.click(refresh)
  expect(api.post).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Requesting refresh…' }).disabled).toBe(true)

  await act(async () => pendingRequest.resolve({
    run_id: 'run-queued', state: 'QUEUED', requested_at: '2026-10-03T12:00:00Z',
    started_at: null, completed_at: null, attempt_count: 0, next_attempt_at: null, failure_code: null,
  }))
})

it('stops polling after a source becomes inaccessible instead of repeatedly probing it', async () => {
  vi.useFakeTimers()
  const queuedCatalog = {
    ...publishedCatalog,
    latest_run: {
      run_id: 'run-queued', state: 'QUEUED', requested_at: '2026-10-03T12:02:00Z',
      started_at: null, completed_at: null, attempt_count: 0, next_attempt_at: null, failure_code: null,
    },
  }
  let statusCalls = 0
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(queuedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) return Promise.resolve(assetPage)
    if (path === '/sources/source-1/catalog-syncs/run-queued') {
      statusCalls += 1
      return Promise.reject({ status: 404, response: { error: { code: 'source_not_found' } } })
    }
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
  renderInspector()

  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  await act(async () => { await vi.advanceTimersByTimeAsync(1_500) })
  expect(statusCalls).toBe(1)
  expect(screen.getByRole('alert').textContent).toContain('saved connection is no longer available')

  await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
  expect(statusCalls).toBe(1)
})

it('ignores a late polling result after the user switches to another source', async () => {
  vi.useFakeTimers()
  const pendingStatus = deferred()
  const queuedCatalog = {
    ...publishedCatalog,
    latest_run: {
      run_id: 'run-queued', state: 'QUEUED', requested_at: '2026-10-03T12:02:00Z',
      started_at: null, completed_at: null, attempt_count: 0, next_attempt_at: null, failure_code: null,
    },
  }
  const secondSource = { source_id: 'source-2', display_name: 'Warehouse analytics' }
  let statusCalls = 0
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') return Promise.resolve(queuedCatalog)
    if (path.startsWith('/sources/source-1/catalog/assets?')) return Promise.resolve(assetPage)
    if (path === '/sources/source-1/catalog-syncs/run-queued') {
      statusCalls += 1
      return pendingStatus.promise
    }
    if (path === '/sources/source-2/catalog') return Promise.resolve({ ...emptyCatalog, source_id: 'source-2' })
    return Promise.resolve({ ...emptyCatalog, source_id: 'source-2' })
  })
  const view = render(<CatalogInspector key={source.source_id} source={source} onClose={vi.fn()} />)

  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  await act(async () => { vi.advanceTimersByTime(1_500) })
  expect(statusCalls).toBe(1)

  view.rerender(<CatalogInspector key={secondSource.source_id} source={secondSource} onClose={vi.fn()} />)
  await act(async () => pendingStatus.resolve({ ...queuedCatalog.latest_run, state: 'SUCCEEDED', completed_at: '2026-10-03T12:03:00Z' }))
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(screen.getByRole('heading', { name: 'Metadata: Warehouse analytics' })).toBeTruthy()
  expect(screen.queryByText(/Metadata synchronization completed/i)).toBeNull()
})

it('reconciles an uncertain refresh request before offering another request', async () => {
  const queuedCatalog = {
    ...emptyCatalog,
    latest_run: {
      run_id: 'run-reconciled', state: 'QUEUED', requested_at: '2026-10-03T12:02:00Z',
      started_at: null, completed_at: null, attempt_count: 0, next_attempt_at: null, failure_code: null,
    },
  }
  let summaries = 0
  api.get.mockImplementation(path => {
    if (path === '/sources/source-1/catalog') {
      summaries += 1
      return Promise.resolve(summaries === 1 ? emptyCatalog : queuedCatalog)
    }
    return Promise.resolve(queuedCatalog.latest_run)
  })
  api.post.mockRejectedValue({ transient: true })
  renderInspector()

  await screen.findByRole('heading', { name: 'Metadata not synchronized' })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh metadata' }))
  await screen.findByText(/could not confirm whether the refresh request was accepted/i)
  expect(api.post).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Refresh in progress' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Refresh in progress' }).disabled).toBe(true)
})
