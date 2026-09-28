import { useState } from 'react'
import { ArrowLeft, ArrowRight, Check, CircleAlert, Mail } from 'lucide-react'
import { Link } from 'react-router-dom'
import { authApi } from '../api/auth'
import PasswordRecoveryShell from '../components/PasswordRecoveryShell'

export default function ForgotPassword() {
  const [email, setEmail] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (event) => {
    event.preventDefault()
    setError('')
    setIsLoading(true)

    try {
      await authApi.forgotPassword(email)
      setSubmitted(true)
    } catch (err) {
      console.error('Forgot password error:', err)
      setError('We could not send a reset link. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }

  if (submitted) {
    return (
      <PasswordRecoveryShell
        icon={Check}
        tone="success"
        eyebrow="Recovery request received"
        title="Check your email"
        description="If an account exists for the email address you entered, we’ve sent a secure password-reset link."
      >
        <div className="recovery-status-copy" role="status" aria-live="polite">
          <strong>Next step:</strong> Open the email and use the link within one hour. For privacy,
          we can’t confirm whether an address is registered.
        </div>

        <div className="recovery-actions">
          <Link to="/login" className="recovery-button">
            Return to log in
            <ArrowRight size={17} aria-hidden="true" />
          </Link>
          <button
            type="button"
            className="recovery-button recovery-button--secondary"
            onClick={() => {
              setSubmitted(false)
              setError('')
            }}
          >
            Use a different email
          </button>
        </div>
      </PasswordRecoveryShell>
    )
  }

  return (
    <PasswordRecoveryShell
      icon={Mail}
      eyebrow="Account recovery"
      title="Reset your password"
      description="Enter the email address associated with your NeuroNova account and we’ll send a secure reset link."
    >
      <form className="recovery-form" onSubmit={handleSubmit}>
        {error && (
          <div id="forgot-password-error" className="recovery-alert" role="alert" aria-live="assertive">
            <CircleAlert size={17} aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        <div className="recovery-field">
          <label className="recovery-label" htmlFor="forgot-password-email">Email address</label>
          <input
            id="forgot-password-email"
            type="email"
            required
            autoComplete="email"
            autoFocus
            placeholder="you@company.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            aria-invalid={Boolean(error)}
            aria-describedby={error ? 'forgot-password-error' : 'forgot-password-hint'}
            className="recovery-input"
          />
          <p id="forgot-password-hint" className="recovery-hint">
            We’ll only use this address to send your recovery instructions.
          </p>
        </div>

        <button type="submit" disabled={isLoading} className="recovery-button">
          {isLoading ? (
            <>
              <span className="recovery-button__spinner" aria-hidden="true" />
              Sending link…
            </>
          ) : (
            <>
              Send reset link
              <ArrowRight size={17} aria-hidden="true" />
            </>
          )}
        </button>
      </form>

      <div className="recovery-footer">
        <Link to="/login" className="recovery-link">
          <ArrowLeft size={16} aria-hidden="true" />
          Back to log in
        </Link>
      </div>
    </PasswordRecoveryShell>
  )
}
