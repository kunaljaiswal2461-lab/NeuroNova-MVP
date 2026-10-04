import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import SavedSourceMaintenance from '../src/components/sources/SavedSourceMaintenance'

vi.mock('../src/api/client', () => ({
  api: {
    post: vi.fn(),
    put: vi.fn(),
  },
}))

const source = {
  source_id: 'saved-source-1',
  display_name: 'Finance warehouse',
  catalog_refresh_enabled: false,
  catalog_refresh_interval_minutes: null,
  catalog_last_success_at: null,
  catalog_last_failure_code: null,
}

const selectedTable = { schema_name: 'reporting', table_name: 'daily_sales' }

function jwtOptions() {
  return expect.objectContaining({ requireJwt: true, preserveErrorBody: true })
}

function renderMaintenance(overrides = {}) {
  return render(
    <SavedSourceMaintenance
      source={source}
      detail={{ selected_tables: [selectedTable] }}
      onUpdated={vi.fn()}
      {...overrides}
    />,
  )
}

beforeEach(() => {
  api.post.mockImplementation(path => {
    if (path === `/sources/${source.source_id}/retest`) {
      return Promise.resolve({
        eligible_tables: [selectedTable],
        source: { ...source, selected_tables: [selectedTable] },
      })
    }
    throw new Error(`unexpected POST ${path}`)
  })
  api.put.mockResolvedValue({ ...source, selected_tables: [selectedTable] })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('retests only the saved source and presents a names-only eligible table scope', async () => {
  const onUpdated = vi.fn()
  renderMaintenance({ onUpdated })

  expect(screen.queryByLabelText(/hostname/i)).toBeNull()
  expect(screen.queryByLabelText(/database name/i)).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: 'Retest saved connection' }))

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${source.source_id}/retest`,
    {},
    jwtOptions(),
  ))
  expect((await screen.findByRole('status')).textContent).toMatch(/Connection retested/i)
  expect(screen.getByRole('checkbox', { name: '"reporting"."daily_sales"' }).checked).toBe(true)
  expect(onUpdated).toHaveBeenCalledWith({ ...source, selected_tables: [selectedTable] })
})

it('clears replacement credentials from form state after a safe successful rotation', async () => {
  const onUpdated = vi.fn()
  const safeUpdatedSource = {
    ...source,
    catalog_refresh_enabled: true,
    catalog_refresh_interval_minutes: 60,
  }
  api.put.mockResolvedValueOnce(safeUpdatedSource)
  renderMaintenance({ onUpdated })

  fireEvent.change(screen.getByLabelText('Replacement username'), { target: { value: 'readonly-next' } })
  fireEvent.change(screen.getByLabelText('Replacement password'), { target: { value: 'replace-me-now' } })
  fireEvent.change(screen.getByLabelText('Replacement custom CA PEM (optional)'), { target: { value: 'certificate-body' } })
  fireEvent.click(screen.getByRole('button', { name: 'Replace credentials' }))

  await waitFor(() => expect(api.put).toHaveBeenCalledWith(
    `/sources/${source.source_id}/credentials`,
    {
      credentials: { username: 'readonly-next', password: 'replace-me-now' },
      tls: { mode: 'verify-full', custom_ca_pem: 'certificate-body' },
    },
    jwtOptions(),
  ))
  expect((await screen.findByRole('status')).textContent).toMatch(/validated and replaced/i)
  expect(onUpdated).toHaveBeenCalledWith(safeUpdatedSource)
  expect(screen.getByLabelText('Replacement username').value).toBe('')
  expect(screen.getByLabelText('Replacement password').value).toBe('')
  expect(screen.getByLabelText('Replacement custom CA PEM (optional)').value).toBe('')
  expect(document.body.textContent).not.toContain('replace-me-now')
  expect(document.body.textContent).not.toContain('certificate-body')
})
