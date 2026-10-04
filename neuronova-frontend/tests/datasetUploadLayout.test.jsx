import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { useDataset } from '../src/context/DatasetContext'
import Upload from '../src/pages/Upload'

const navigate = vi.fn()

vi.mock('../src/api/client', () => ({ api: { get: vi.fn(), postForm: vi.fn() } }))
vi.mock('../src/context/DatasetContext', () => ({ useDataset: vi.fn() }))
vi.mock('react-router-dom', () => ({ useNavigate: () => navigate }))
vi.mock('../src/components/Layout', () => ({
  default: ({ actions, children, className }) => (
    <div className={className}>
      <div>{actions}</div>
      <main className="page-area">{children}</main>
    </div>
  ),
}))

const completeDataset = {
  dataset_id: 'dataset-1',
  status: 'COMPLETE',
  original_name: 'sample.csv',
  progress_pct: 100,
}

beforeEach(() => {
  useDataset.mockReturnValue({
    activeDataset: completeDataset,
    activeDatasetId: 'dataset-1',
    selectDataset: vi.fn(),
    clearDataset: vi.fn(),
    statusMessage: null,
  })
  api.get.mockResolvedValue({ items: [] })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('keeps the focused upload workflow, compact pipeline, and existing completion actions', async () => {
  const view = render(<Upload />)

  await screen.findByText('No datasets yet')

  const workflow = view.container.querySelector('.upload-workflow')
  const dropzone = view.container.querySelector('.upload-zone')
  expect(workflow).not.toBeNull()
  expect(dropzone).not.toBeNull()
  expect(view.container.querySelector('.upload-pipeline-card')).not.toBeNull()
  expect(view.container.querySelector('.upload-pipeline-steps')).not.toBeNull()
  expect(view.container.querySelector('.upload-recent-card')).not.toBeNull()
  expect(view.container.querySelector('.upload-recent-table-scroll')).not.toBeNull()
  expect(view.container.querySelector('.upload-empty-state')).not.toBeNull()
  expect(view.container.querySelectorAll('.progress-step')).toHaveLength(9)
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100')
  expect(screen.getAllByText('Done')).toHaveLength(9)
  expect(screen.getByLabelText('Upload dataset').getAttribute('accept')).toBe('.csv,.xlsx,.json,.parquet')

  fireEvent.dragOver(dropzone)
  expect(dropzone.classList.contains('drag-active')).toBe(true)
  fireEvent.dragLeave(dropzone)
  expect(dropzone.classList.contains('drag-active')).toBe(false)

  fireEvent.click(screen.getByRole('button', { name: 'Explore Dataset →' }))
  fireEvent.click(screen.getByRole('button', { name: 'View Insights' }))
  expect(navigate).toHaveBeenCalledWith('/explorer')
  expect(navigate).toHaveBeenCalledWith('/insights')

  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
})

it('keeps a temporary backend retry message separate from the empty dataset state', async () => {
  api.get.mockRejectedValueOnce(new Error('Backend is temporarily unavailable. Retrying...'))
  const view = render(<Upload />)

  const retryMessage = await screen.findByText('Backend is temporarily unavailable. Retrying...')
  expect(retryMessage.closest('.upload-list-status')).not.toBeNull()
  expect(view.container.querySelector('.upload-empty-state')).not.toBeNull()
  expect(screen.getByText('No datasets yet')).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
})
