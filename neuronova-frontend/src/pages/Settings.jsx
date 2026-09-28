import { useState } from 'react'
import Layout from '../components/Layout'
import { useAuth } from '../context/AuthContext'

const CONFIRMATION_TEXT = 'DELETE'

export default function Settings() {
  const { user, deleteAccount } = useAuth()
  const [isDialogOpen, setIsDialogOpen] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [isDeleting, setIsDeleting] = useState(false)
  const [error, setError] = useState(null)

  const closeDialog = () => {
    if (isDeleting) return
    setIsDialogOpen(false)
    setConfirmation('')
    setError(null)
  }

  const handleDelete = async event => {
    event.preventDefault()
    if (confirmation !== CONFIRMATION_TEXT || isDeleting) return

    setIsDeleting(true)
    setError(null)
    try {
      await deleteAccount(confirmation)
    } catch (requestError) {
      setError(requestError.message || 'Unable to delete your account. Please try again.')
      setIsDeleting(false)
    }
  }

  return (
    <Layout title="Settings" subtitle="Manage your account">
      <div style={{ maxWidth: 760, margin: '0 auto' }}>
        <section style={{ background: 'white', border: '1px solid #FECACA', borderRadius: 12, padding: '28px 30px', boxShadow: 'var(--shadow-card)' }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
            <div style={{ width: 38, height: 38, borderRadius: 10, background: '#FFF1F2', color: '#BE123C', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="M19 6l-1 14H6L5 6" /><path d="M10 11v5M14 11v5" />
              </svg>
            </div>
            <div>
              <h2 style={{ margin: 0, fontFamily: 'var(--font-heading)', fontSize: 18, fontWeight: 700, color: '#881337' }}>Delete account</h2>
              <p style={{ margin: '8px 0 0', maxWidth: 560, fontFamily: 'var(--font-body)', fontSize: 14, lineHeight: 1.6, color: '#9F1239' }}>
                Permanently delete {user?.email ? `${user.email} and all associated` : 'your'} datasets, reports, and conversations. This cannot be undone.
              </p>
              <button
                type="button"
                onClick={() => setIsDialogOpen(true)}
                style={{ marginTop: 18, minHeight: 38, padding: '0 14px', border: '1px solid #E11D48', borderRadius: 8, background: '#FFFFFF', color: '#BE123C', fontFamily: 'var(--font-body)', fontSize: 13, fontWeight: 700, cursor: 'pointer' }}
              >
                Delete account
              </button>
            </div>
          </div>
        </section>
      </div>

      {isDialogOpen && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 200, display: 'grid', placeItems: 'center', padding: 16, background: 'rgba(15, 23, 42, 0.56)' }}>
          <div role="dialog" aria-modal="true" aria-labelledby="delete-account-title" aria-describedby="delete-account-description" style={{ width: 'min(100%, 500px)', background: 'white', borderRadius: 14, padding: '28px', boxShadow: '0 24px 60px rgba(15, 23, 42, 0.28)' }}>
            <h2 id="delete-account-title" style={{ margin: 0, fontFamily: 'var(--font-heading)', fontSize: 20, fontWeight: 700, color: '#881337' }}>Delete your account?</h2>
            <p id="delete-account-description" style={{ margin: '10px 0 18px', fontFamily: 'var(--font-body)', fontSize: 14, lineHeight: 1.6, color: 'var(--color-text-secondary)' }}>
              This permanently removes your account and its data. To continue, type <strong>{CONFIRMATION_TEXT}</strong> below.
            </p>
            <form onSubmit={handleDelete}>
              <label htmlFor="delete-account-confirmation" style={{ display: 'block', marginBottom: 7, fontFamily: 'var(--font-body)', fontSize: 12, fontWeight: 700, color: 'var(--color-text-primary)' }}>
                Confirmation
              </label>
              <input
                id="delete-account-confirmation"
                autoFocus
                value={confirmation}
                onChange={event => setConfirmation(event.target.value)}
                placeholder={CONFIRMATION_TEXT}
                disabled={isDeleting}
                style={{ width: '100%', height: 42, padding: '0 12px', border: '1px solid var(--color-border)', borderRadius: 8, fontFamily: 'var(--font-data)', fontSize: 14, color: 'var(--color-text-primary)', outline: 'none' }}
              />
              {error && (
                <p role="alert" style={{ margin: '12px 0 0', fontFamily: 'var(--font-body)', fontSize: 13, color: '#BE123C' }}>{error}</p>
              )}
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 22 }}>
                <button type="button" onClick={closeDialog} disabled={isDeleting} style={{ height: 38, padding: '0 14px', border: '1px solid var(--color-border)', borderRadius: 8, background: 'white', color: 'var(--color-text-secondary)', fontFamily: 'var(--font-body)', fontSize: 13, fontWeight: 600, cursor: isDeleting ? 'not-allowed' : 'pointer' }}>
                  Cancel
                </button>
                <button type="submit" disabled={confirmation !== CONFIRMATION_TEXT || isDeleting} style={{ height: 38, padding: '0 14px', border: 'none', borderRadius: 8, background: confirmation === CONFIRMATION_TEXT && !isDeleting ? '#E11D48' : '#FDA4AF', color: 'white', fontFamily: 'var(--font-body)', fontSize: 13, fontWeight: 700, cursor: confirmation === CONFIRMATION_TEXT && !isDeleting ? 'pointer' : 'not-allowed' }}>
                  {isDeleting ? 'Deleting account…' : 'Permanently delete'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </Layout>
  )
}
