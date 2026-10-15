import { useEffect, useState } from 'react'
import { SHOW_ELAPSED_AFTER_MS, stageLabel } from './stageLabel'

// The stage of the turn that is running (SPEC-10 W2.2). Only the label text changes between stages, so a screen
// reader announces each stage once; the elapsed counter is hidden from assistive technology.
export default function TurnProgress({ stage, startedAt, sourceName, reasonCode, typed = false }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!stage) return undefined
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [stage, startedAt])
  if (!stage) return null
  const elapsedMs = Math.max(0, now - (startedAt ?? now))
  const label = stageLabel({ stage, sourceName, reasonCode, typed, elapsedMs })
  if (!label) return null
  return (
    <div role="status" aria-live="polite" style={{ marginBottom: 8, padding: '7px 10px', background: '#EFF6FF', border: '1px solid #BFDBFE', borderRadius: 6, fontFamily: 'var(--font-data)', fontSize: 11, color: '#1D4ED8' }}>
      <span>{label}</span>
      {elapsedMs >= SHOW_ELAPSED_AFTER_MS && <span aria-hidden="true" style={{ color: 'var(--color-text-muted)' }}> · {Math.floor(elapsedMs / 1000)} s</span>}
    </div>
  )
}
