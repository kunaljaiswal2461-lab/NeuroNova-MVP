// Per-column display facts, computed once per result (SPEC-10 W4.3, W4.4).
import { columnsOf, kindOf } from '../eligibility'
import { FALLBACK_TIME_ZONE } from './locale'
import { granularityOf, parseTemporal, zoneName } from './temporal'

const TEMPORAL = new Set(['date', 'timestamp', 'timestamptz'])
const cache = new WeakMap()

export function columnProfile(result, columnIndex, timeZone = FALLBACK_TIME_ZONE) {
  if (!result || typeof result !== 'object') return { kind: null, granularity: null, timeZone }
  let byKey = cache.get(result)
  if (!byKey) { byKey = new Map(); cache.set(result, byKey) }
  const key = `${columnIndex}|${timeZone}`
  if (byKey.has(key)) return byKey.get(key)
  const column = columnsOf(result)[columnIndex]
  const kind = column ? kindOf(column[1]) : null
  const profile = { kind, granularity: null, timeZone }
  if (TEMPORAL.has(kind)) {
    const zone = kind === 'timestamptz' ? timeZone : 'UTC'
    const instants = []
    for (const row of Array.isArray(result.rows) ? result.rows : []) {
      const cell = row?.[columnIndex]
      if (!cell || cell.value == null || typeof cell.value !== 'string') continue
      const parsed = parseTemporal(cell.value, kind)
      if (parsed) instants.push(parsed.ms)
    }
    profile.granularity = granularityOf(instants, zone, kind)
  }
  byKey.set(key, profile)
  return profile
}

export function columnHint(profile) {
  switch (profile?.kind) {
    case 'integer': case 'decimal': case 'number': return 'number'
    case 'date': return 'date'
    case 'timestamp': return ['year', 'month', 'day'].includes(profile.granularity) ? 'date' : 'date & time'
    case 'timestamptz': {
      const zone = zoneName(profile.timeZone)
      return `${['year', 'month', 'day'].includes(profile.granularity) ? 'date' : 'date & time'} (${zone})`
    }
    case 'boolean': return 'true/false'
    case 'uuid': return 'id'
    case 'text': return 'text'
    default: return ''
  }
}

// The pass-2 label when there is one; otherwise the alias made readable (display only; the alias stays in `title`).
export function columnTitle(alias, tableColumns) {
  const named = Array.isArray(tableColumns) ? tableColumns.find(item => item.column === alias) : null
  if (named?.label) return named.label
  const spaced = String(alias ?? '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim()
  return spaced ? spaced[0].toUpperCase() + spaced.slice(1) : String(alias ?? '')
}
