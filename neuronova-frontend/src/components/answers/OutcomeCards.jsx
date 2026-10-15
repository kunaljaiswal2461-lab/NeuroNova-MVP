import SqlPanel from './SqlPanel'

// One card per turn outcome that is not an answer (SPEC-10 W5.6–W5.8, D5). All text is plain.
import { canTryAgain } from './outcomes'

const box = tone => ({
  margin: 0, padding: '10px 12px', borderRadius: 8, fontFamily: 'var(--font-body)', fontSize: 13, lineHeight: 1.55,
  ...(tone === 'error' ? { background: '#FFF1F2', border: '1px solid #FECDD3', color: '#9F1239' }
    : tone === 'warn' ? { background: '#FFFBEB', border: '1px solid #FDE68A', color: '#92400E' }
      : { background: '#EFF6FF', border: '1px solid #BFDBFE', color: '#1D4ED8' }),
})
const hint = { margin: '6px 0 0', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)' }

export function ClarificationCard({ text }) {
  return (
    <section aria-label="Clarification needed" style={{ display: 'grid', gap: 6 }}>
      <p style={{ ...box('info'), color: 'var(--color-text-primary)', whiteSpace: 'pre-wrap' }}>{text}</p>
      <p style={{ ...hint, margin: 0 }}>Reply below — your reply is read together with this question.</p>
    </section>
  )
}

export function UnsupportedCard({ text, writeRefusal = false }) {
  return (
    <section aria-label="Not supported" style={{ display: 'grid', gap: 6 }}>
      <p style={{ ...box('warn'), whiteSpace: 'pre-wrap' }}>{text}</p>
      {!writeRefusal && <p style={{ ...hint, margin: 0 }}>Try asking for a read-only summary of the data instead.</p>}
    </section>
  )
}

export function FailedCard({ text, answer, failureCode, onEdit, onTryAgain, busy }) {
  return (
    <section aria-label="Could not answer" style={{ display: 'grid', gap: 8 }}>
      <p role="status" style={{ ...box('error'), whiteSpace: 'pre-wrap' }}>{text}</p>
      <SqlPanel sql={answer?.sql} firstSql={answer?.firstSql} repair={answer?.repair} title="Query that was tried" />
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {onEdit && <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={onEdit}>Edit question</button>}
        {onTryAgain && canTryAgain(failureCode) && !busy && <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={onTryAgain}>Try again</button>}
      </div>
    </section>
  )
}

export function CancelledNote({ text }) {
  return <p role="status" style={{ ...box('info'), color: 'var(--color-text-secondary)' }}>{text || 'Cancelled.'}</p>
}

export function InterruptedNote({ text }) {
  return <p role="status" style={box('warn')}>{text}</p>
}
