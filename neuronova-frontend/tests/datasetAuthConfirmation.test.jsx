import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { useAuth } from '../src/context/AuthContext'
import { authApi } from '../src/api/auth'
import ForgotPassword from '../src/pages/ForgotPassword'
import Register from '../src/pages/Register'
import ResetPassword from '../src/pages/ResetPassword'
import VerifyEmailLanding from '../src/pages/VerifyEmailLanding'

vi.mock('../src/context/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('../src/api/auth', () => ({
  authApi: {
    forgotPassword: vi.fn(),
    resetPassword: vi.fn(),
  },
}))

const register = vi.fn()
const login = vi.fn()

beforeEach(() => {
  register.mockResolvedValue(undefined)
  useAuth.mockReturnValue({ register, login })
  authApi.forgotPassword.mockResolvedValue(undefined)
  authApi.resetPassword.mockResolvedValue(undefined)
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

it('keeps the password-reset confirmation privacy-preserving and actionable', async () => {
  render(
    <MemoryRouter initialEntries={['/auth/forgot']}>
      <ForgotPassword />
    </MemoryRouter>,
  )

  fireEvent.change(screen.getByLabelText('Email address'), {
    target: { value: 'jane@example.com' },
  })
  fireEvent.click(screen.getByRole('button', { name: /send reset link/i }))

  expect(await screen.findByRole('heading', { name: 'Check your email' })).toBeTruthy()
  expect(screen.getByText(/can’t confirm whether an address is registered/i)).toBeTruthy()
  expect(screen.getByRole('link', { name: /return to log in/i })).toBeTruthy()
})

it('shows a completion state after a valid password reset', async () => {
  render(
    <MemoryRouter initialEntries={['/auth/reset-password?token=reset-token']}>
      <ResetPassword />
    </MemoryRouter>,
  )

  fireEvent.change(screen.getByLabelText('New password'), {
    target: { value: 'password123' },
  })
  fireEvent.change(screen.getByLabelText('Confirm new password'), {
    target: { value: 'password123' },
  })
  fireEvent.click(screen.getByRole('button', { name: /reset password/i }))

  expect(await screen.findByRole('heading', { name: 'You’re all set' })).toBeTruthy()
  expect(authApi.resetPassword).toHaveBeenCalledWith('reset-token', 'password123')
})
