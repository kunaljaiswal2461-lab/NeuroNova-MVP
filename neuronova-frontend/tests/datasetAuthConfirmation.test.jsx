import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { useAuth } from '../src/context/AuthContext'
import Register from '../src/pages/Register'
import VerifyEmailLanding from '../src/pages/VerifyEmailLanding'

vi.mock('../src/context/AuthContext', () => ({ useAuth: vi.fn() }))

const register = vi.fn()
const login = vi.fn()

beforeEach(() => {
  register.mockResolvedValue(undefined)
  useAuth.mockReturnValue({ register, login })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('shows a pending-verification confirmation after email registration', async () => {
  render(
    <MemoryRouter initialEntries={['/register']}>
      <Routes>
        <Route path="/register" element={<Register />} />
        <Route path="/auth/verified" element={<VerifyEmailLanding />} />
      </Routes>
    </MemoryRouter>,
  )

  fireEvent.change(screen.getByPlaceholderText('Jane Doe'), { target: { value: 'Jane Doe' } })
  fireEvent.change(screen.getByPlaceholderText('you@work-email.com'), {
    target: { value: 'jane@example.com' },
  })
  fireEvent.change(screen.getByPlaceholderText('Create a strong password'), {
    target: { value: 'password123' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Get Early Access' }))

  expect(await screen.findByRole('heading', { name: 'Check your inbox' })).toBeTruthy()
  expect(screen.queryByRole('heading', { name: 'Email verified' })).toBeNull()
  expect(login).not.toHaveBeenCalled()
})

it('reserves the verified confirmation for a completed verification link', () => {
  render(
    <MemoryRouter initialEntries={['/auth/verified']}>
      <VerifyEmailLanding />
    </MemoryRouter>,
  )

  expect(screen.getByRole('heading', { name: 'Email verified' })).toBeTruthy()
})
