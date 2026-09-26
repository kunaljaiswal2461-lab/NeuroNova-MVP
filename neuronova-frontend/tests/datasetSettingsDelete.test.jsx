import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useAuth } from '../src/context/AuthContext'
import Settings from '../src/pages/Settings'

vi.mock('../src/context/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('../src/components/Layout', () => ({
  default: ({ children }) => <main>{children}</main>,
}))

const deleteAccount = vi.fn()

beforeEach(() => {
  deleteAccount.mockResolvedValue(undefined)
  useAuth.mockReturnValue({
    user: { email: 'analyst@example.com' },
    deleteAccount,
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('requires typed confirmation before deleting an account', async () => {
  render(<Settings />)

  fireEvent.click(screen.getByRole('button', { name: 'Delete account' }))
  expect(screen.getByRole('dialog')).toBeTruthy()

  const confirmButton = screen.getByRole('button', { name: 'Permanently delete' })
  expect(confirmButton.disabled).toBe(true)

  fireEvent.change(screen.getByLabelText('Confirmation'), { target: { value: 'DELETE' } })
  expect(confirmButton.disabled).toBe(false)

  fireEvent.click(confirmButton)
  await waitFor(() => expect(deleteAccount).toHaveBeenCalledWith('DELETE'))
})
