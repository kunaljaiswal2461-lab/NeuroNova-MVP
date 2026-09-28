import { useState } from 'react'
import { ArrowLeft, ArrowRight, Check, CircleAlert, Eye, EyeOff, KeyRound } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { authApi } from '../api/auth'
import PasswordRecoveryShell from '../components/PasswordRecoveryShell'

export default function ResetPassword() {
  const [searchParams] = useSearchParams()
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [completed, setCompleted] = useState(false)
  const [showPasswords, setShowPasswords] = useState(false)
  const token = searchParams.get('token')

  const hasMinimumLength = password.length >= 8
  const passwordsMatch = confirmPassword.length > 0 && password === confirmPassword

  const handleSubmit = async (event) => {
    event.preventDefault()
    setError('')

    if (password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }

    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }

    setIsLoading(true)
    try {
      await authApi.resetPassword(token, password)
      setCompleted(true)
    } catch (err) {
      setError(err.message || 'Failed to reset password. The link may have expired.')
    } finally {
      setIsLoading(false)
    }
  }

  if (!token) {
    return (
      <PasswordRecoveryShell
        icon={CircleAlert}
        tone="danger"
        eyebrow="Recovery link unavailable"
        title="This reset link is invalid"
        description="The link is missing, malformed, or has expired. Request a new one to continue."
      >
        <div className="recovery-status-copy">
          Password reset links are valid for one hour. Requesting a new link is the safest way to continue.
        </div>

        <div className="recovery-actions">
          <Link to="/auth/forgot" className="recovery-button">
            Request a new link
            <ArrowRight size={17} aria-hidden="true" />
          </Link>
          <Link to="/login" className="recovery-button recovery-button--secondary">
            Back to log in
          </Link>
        </div>
      </PasswordRecoveryShell>
    )
  }

  if (completed) {
    return (
      <PasswordRecoveryShell
        icon={Check}
        tone="success"
        eyebrow="Password updated"
        title="You’re all set"
        description="Your password has been changed securely. Use your new password the next time you sign in."
      >
        <div className="recovery-status-copy" role="status" aria-live="polite">
          <strong>Account security refreshed.</strong> Your current browser session has not been signed in.
        </div>

        <div className="recovery-actions">
          <Link to="/login" className="recovery-button">
            Continue to log in
            <ArrowRight size={17} aria-hidden="true" />
          </Link>
        </div>
      </PasswordRecoveryShell>
    )
  }

  return (
    <PasswordRecoveryShell
      icon={KeyRound}
      eyebrow="Set a new password"
      title="Choose a new password"
      description="Use a password you don’t use elsewhere to keep your NeuroNova account protected."
    >
      <form className="recovery-form" onSubmit={handleSubmit}>
        {error && (
          <div id="reset-password-error" className="recovery-alert" role="alert" aria-live="assertive">
            <CircleAlert size={17} aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        <div className="recovery-field">
          <label className="recovery-label" htmlFor="new-password">New password</label>
          <div className="recovery-input-wrap">
            <input
              id="new-password"
              type={showPasswords ? 'text' : 'password'}
              required
              minLength={8}
              autoComplete="new-password"
              autoFocus
              placeholder="Enter a new password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value)
                setError('')
              }}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? 'reset-password-error password-requirements' : 'password-requirements'}
              className="recovery-input recovery-input--with-action"
            />
            <button
              type="button"
              className="recovery-password-toggle"
              onClick={() => setShowPasswords((visible) => !visible)}
              aria-label={showPasswords ? 'Hide passwords' : 'Show passwords'}
              aria-pressed={showPasswords}
            >
              {showPasswords ? <EyeOff size={18} aria-hidden="true" /> : <Eye size={18} aria-hidden="true" />}
            </button>
          </div>
        </div>

        <div className="recovery-field">
          <label className="recovery-label" htmlFor="confirm-password">Confirm new password</label>
          <input
            id="confirm-password"
            type={showPasswords ? 'text' : 'password'}
            required
            minLength={8}
            autoComplete="new-password"
            placeholder="Re-enter your new password"
            value={confirmPassword}
            onChange={(event) => {
              setConfirmPassword(event.target.value)
              setError('')
            }}
            aria-invalid={Boolean(error)}
            aria-describedby={error ? 'reset-password-error password-requirements' : 'password-requirements'}
            className="recovery-input"
          />
        </div>

        <div id="password-requirements" className="recovery-requirements" aria-live="polite">
          <p className={`recovery-requirement ${hasMinimumLength ? 'recovery-requirement--met' : ''}`}>
            <Check size={15} aria-hidden="true" />
            Use at least 8 characters
          </p>
          <p className={`recovery-requirement ${passwordsMatch ? 'recovery-requirement--met' : ''}`}>
            <Check size={15} aria-hidden="true" />
            Passwords match
          </p>
        </div>

        <button type="submit" disabled={isLoading} className="recovery-button">
          {isLoading ? (
            <>
              <span className="recovery-button__spinner" aria-hidden="true" />
              Updating password…
            </>
          ) : (
            <>
              Reset password
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
