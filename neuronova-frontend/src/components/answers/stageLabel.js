import { repairReasonLabel } from './repairLabels'

// Stage -> text (SPEC-10 W2.2).
export const WAKE_AFTER_MS = 8_000
export const SHOW_ELAPSED_AFTER_MS = 10_000

export function stageLabel({ stage, sourceName, reasonCode, typed, elapsedMs }) {
  switch (stage) {
    case 'sending':
      return elapsedMs >= WAKE_AFTER_MS ? 'Waking the server — the first question can take up to a minute.' : 'Sending your question…'
    case 'understanding': return 'Understanding the question…'
    case 'written': return 'Query written · checking it…'
    case 'running': return typed || !sourceName ? 'Running the query…' : `Running the query on ${sourceName}…`
    case 'repairing': return `The first query failed (${repairReasonLabel(reasonCode)}) · correcting it once…`
    case 'corrected': return 'Corrected query written · checking it…'
    case 'presenting': return 'Writing the answer…'
    default: return null
  }
}

