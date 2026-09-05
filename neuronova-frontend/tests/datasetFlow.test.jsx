import { useEffect } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { DatasetProvider, useDataset } from '../src/context/DatasetContext'
import DatasetRoute from '../src/components/DatasetRoute'
import Sidebar from '../src/components/Sidebar'
import Upload from '../src/pages/Upload'
import { MAX_UPLOAD_BYTES, UPLOAD_LIMIT_MESSAGE } from '../src/context/datasetStatus'

vi.mock('../src/api/client', () => ({ api: { get: vi.fn(), postForm: vi.fn() } }))
let context
let navigate
function Probe() {
  context = useDataset()
  navigate = useNavigate()
  return null
}
function setup(children, path = '/upload') {
  return render(<MemoryRouter initialEntries={[path]}><DatasetProvider><Probe />{children}</DatasetProvider></MemoryRouter>)
}
const settle = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }
const select = async dataset => { await act(async () => context.selectDataset(dataset)) }
const completed = { dataset_id: 'old', status: 'COMPLETE', original_name: 'old.csv' }
const queued = { dataset_id: 'new', status: 'QUEUED', original_name: 'new.csv', progress_pct: 0 }
const chooseFile = size => {
  const file = new File(['csv'], 'new.csv', { type: 'text/csv' })
  Object.defineProperty(file, 'size', { value: size })
  fireEvent.change(screen.getByLabelText('Upload dataset'), { target: { files: [file] } })
}

beforeEach(() => {
  vi.useFakeTimers()
  api.get.mockImplementation(path => Promise.resolve(path.endsWith('/status') ? queued : { items: [] }))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })

it('rejects oversized files before POST without disturbing completed selection', async () => {
  setup(<Upload />)
  await select(completed)
  chooseFile(MAX_UPLOAD_BYTES + 1)
  expect(screen.getByRole('alert').textContent).toContain(UPLOAD_LIMIT_MESSAGE)
  expect(api.postForm).not.toHaveBeenCalled()
  expect(context.activeDatasetId).toBe('old')
})

it('locks immediately, accepts the boundary, selects acknowledgement, and starts one immediate poll', async () => {
  let acknowledge
  api.postForm.mockImplementation(() => new Promise(resolve => { acknowledge = resolve }))
  setup(<Upload />)
  await select(completed)
  chooseFile(MAX_UPLOAD_BYTES)
  expect(context.activeDataset.status).toBe('UPLOADING')
  expect(context.datasetReady).toBe(false)
  expect(screen.getByRole('button', { name: 'Explorer' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'New Dataset' }).disabled).toBe(true)
  expect(api.postForm).toHaveBeenCalledTimes(1)
  await act(async () => acknowledge({ dataset_id: 'new', status: 'QUEUED' }))
  expect(context.activeDatasetId).toBe('new')
  expect(api.get.mock.calls.filter(([path]) => path.endsWith('/status'))).toHaveLength(1)
})

it('ignores upload acknowledgement after page unmount and aborts the request', async () => {
  let acknowledge
  api.postForm.mockImplementation(() => new Promise(resolve => { acknowledge = resolve }))
  const view = setup(<Upload />)
  chooseFile(100)
  const signal = api.postForm.mock.calls[0][2].signal
  view.unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => acknowledge(queued))
  expect(api.get.mock.calls.filter(([path]) => path.endsWith('/status'))).toHaveLength(0)
})

it.each(['/explorer', '/insights', '/visualization', '/chat', '/explorer/', '/EXPLORER', '/EXPLORER/'])('guards direct route %s, then auto-unlocks only on COMPLETE', async path => {
  const artifactFetch = vi.fn()
  function Analytics() { useEffect(() => { artifactFetch() }, []); return <p>Analytics content</p> }
  api.get.mockImplementation(url => Promise.resolve(url.endsWith('/status')
    ? { ...queued, status: 'PROFILING', progress_pct: 30 }
    : { ...queued, status: 'COMPLETE', row_count: 25, col_count: 3 }))
  setup(<Routes><Route path={path.toLowerCase().replace(/\/+$/, '')} element={<DatasetRoute><Analytics /></DatasetRoute>} /></Routes>, path)
  await select(queued)
  expect(artifactFetch).not.toHaveBeenCalled()
  for (const name of ['Explorer', 'Insights', 'Visualization', 'Chat']) {
    expect(screen.getByRole('button', { name }).disabled).toBe(true)
  }
  api.get.mockImplementation(url => Promise.resolve(url.endsWith('/status')
    ? { status: 'COMPLETE', progress_pct: 100 }
    : { ...queued, status: 'COMPLETE', row_count: 25, col_count: 3 }))
  await act(async () => vi.advanceTimersByTimeAsync(5000))
  expect(artifactFetch).toHaveBeenCalledTimes(1)
  expect(context.activeDataset.row_count).toBe(25)
  expect(context.datasetReady).toBe(true)
})

it.each(['/upload', '/upload/', '/EXPLORER/'])('shares polling across consumers and cancels on dataset switch and exit from %s', async path => {
  let resolveOld
  api.get.mockImplementation(path => path.includes('/first/')
    ? new Promise(resolve => { resolveOld = resolve }) : Promise.resolve(queued))
  setup(<><Sidebar /><DatasetRoute><p>Ready</p></DatasetRoute></>, path)
  await select({ ...queued, dataset_id: 'first' })
  const oldSignal = api.get.mock.calls[0][1].signal
  await select(queued)
  expect(oldSignal.aborted).toBe(true)
  await act(async () => resolveOld({ status: 'COMPLETE' }))
  expect(context.activeDatasetId).toBe('new')
  expect(context.datasetReady).toBe(false)
  const newSignal = api.get.mock.calls[1][1].signal
  await act(async () => navigate('/'))
  expect(newSignal.aborted).toBe(true)
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(api.get).toHaveBeenCalledTimes(2)
})

it('preserves health when completion metadata arrives and rejects stale health updates', async () => {
  let metadata
  api.get.mockImplementation(path => path.endsWith('/status')
    ? Promise.resolve({ status: 'COMPLETE', progress_pct: 100 })
    : new Promise(resolve => { metadata = resolve }))
  setup(<Probe />)
  await select(queued)
  await act(async () => context.updateDataset('new', { _health: { score: 90, components: {} } }))
  await act(async () => metadata({ ...queued, status: 'COMPLETE', row_count: 50 }))
  expect(context.activeDataset._health.score).toBe(90)
  expect(context.activeDataset.row_count).toBe(50)
  await select(completed)
  await act(async () => context.updateDataset('new', { _health: { score: 1 } }))
  expect(context.activeDatasetId).toBe('old')
  expect(context.activeDataset._health).toBeUndefined()
})

it('shows FAILED with reupload action and no continued polls', async () => {
  api.get.mockImplementation(path => Promise.resolve(path.endsWith('/status')
    ? { status: 'FAILED', error_message: 'Processing was interrupted. Please upload the dataset again.' }
    : { items: [] }))
  setup(<Upload />)
  await select(queued)
  await settle()
  expect(screen.getByRole('alert').textContent).toContain('Processing was interrupted.')
  expect(screen.getByRole('button', { name: 'Upload dataset again' })).toBeTruthy()
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(api.get.mock.calls.filter(([path]) => path.endsWith('/status'))).toHaveLength(1)
  expect(context.datasetReady).toBe(false)
})

it('shows an already-failed dataset with its message and progress without polling', async () => {
  const failed = { dataset_id: 'fl', status: 'FAILED', progress_pct: 30, response: undefined,
    error_message: 'Processing was interrupted. Please upload the dataset again.', original_name: 'fail.csv' }
  setup(<DatasetRoute><p>Analytics</p></DatasetRoute>, '/explorer')
  await select(failed)
  await settle()
  expect(screen.getByText(/Processing was interrupted\./)).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Failed' })).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Upload dataset again' })).toBeTruthy()
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(api.get.mock.calls.filter(([path]) => path.endsWith('/status'))).toHaveLength(0)
  expect(context.datasetReady).toBe(false)
})

it('keeps queued status during retry notices and distinguishes unavailable from FAILED', async () => {
  let statusOptions
  api.get.mockImplementation((path, options) => {
    if (path.endsWith('/status')) {
      statusOptions = options
      return new Promise(() => {})
    }
    return Promise.resolve({ items: [] })
  })
  setup(<Upload />)
  await select(queued)
  await act(async () => statusOptions.onRetry('Backend is temporarily unavailable. Retrying...'))
  expect(screen.getByText('Backend is temporarily unavailable. Retrying...')).toBeTruthy()
  expect(context.activeDataset.status).toBe('QUEUED')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(context.datasetReady).toBe(false)
})

it('ignores late completion metadata after another dataset is selected', async () => {
  let metadata
  api.get.mockImplementation(path => path.endsWith('/status')
    ? Promise.resolve({ status: 'COMPLETE', progress_pct: 100 })
    : new Promise(resolve => { metadata = resolve }))
  setup(<Probe />)
  await select(queued)
  const signal = api.get.mock.calls[1][1].signal
  await select(completed)
  expect(signal.aborted).toBe(true)
  await act(async () => metadata({ ...queued, status: 'COMPLETE', row_count: 50 }))
  expect(context.activeDatasetId).toBe('old')
  expect(context.activeDataset.row_count).toBeUndefined()
})

it('uses only reupload after failure, without automatically replaying POST', async () => {
  api.postForm.mockRejectedValue(new Error('Upload outcome is uncertain. Check Recent Datasets before uploading again.'))
  setup(<Upload />)
  chooseFile(100)
  await settle()
  expect(screen.getByRole('alert').textContent).toContain('Check Recent Datasets')
  expect(context.activeDataset).toBeNull()
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(api.postForm).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Choose a file to retry' }).disabled).toBe(false)
})


it.each(['/upload/', '/UPLOAD', '/UPLOAD/'])('polls an acknowledged upload on %s and auto-unlocks navigation', async path => {
  api.postForm.mockResolvedValue(queued)
  setup(<Routes><Route path="/upload" element={<Upload />} /></Routes>, path)
  chooseFile(100)
  await settle()
  expect(context.activeDatasetId).toBe('new')
  expect(api.get.mock.calls.filter(([url]) => url.endsWith('/status'))).toHaveLength(1)
  expect(screen.getByRole('button', { name: 'Explorer' }).disabled).toBe(true)
  api.get.mockImplementation(url => Promise.resolve(url === '/datasets'
    ? { items: [] } : { ...queued, status: 'COMPLETE', progress_pct: 100 }))
  await act(async () => vi.advanceTimersByTimeAsync(5000))
  expect(context.datasetReady).toBe(true)
  expect(screen.getByRole('link', { name: 'Explorer' })).toBeTruthy()
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100')
})

it.each(['/explorer/extra', '/uploading'])('does not poll outside a matching workspace route: %s', async path => {
  setup(<Probe />, path)
  await select(queued)
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(api.get).not.toHaveBeenCalled()
})
