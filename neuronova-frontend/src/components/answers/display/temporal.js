// Temporal parsing and formatting by explicit time zone (SPEC-10 D4). `date` and naive `timestamp` values are wall
// times: they are read as UTC and shown in UTC so the browser's own zone can never shift them.
import { DISPLAY_LOCALE } from './locale'

const ISO = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/i

export function parseTemporal(exact, kind) {
  const match = ISO.exec(String(exact ?? '').trim())
  if (!match) return null
  const [, y, mo, d, h = '00', mi = '00', s = '00', zone] = match
  const wall = Date.UTC(+y, +mo - 1, +d, +h, +mi, +s)
  if (Number.isNaN(wall)) return null
  if (kind !== 'timestamptz') return { ms: wall, zone: 'UTC' }
  if (!zone || /^z$/i.test(zone)) return { ms: wall, zone: 'UTC' }
  const sign = zone[0] === '-' ? -1 : 1
  const digits = zone.slice(1).replace(':', '')
  return { ms: wall - sign * (+digits.slice(0, 2) * 60 + +digits.slice(2)) * 60_000, zone }
}

const formatters = new Map()
function formatter(timeZone, withName) {
  const key = `${timeZone}|${withName}`
  if (!formatters.has(key)) {
    formatters.set(key, new Intl.DateTimeFormat(DISPLAY_LOCALE, {
      timeZone, day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23', ...(withName ? { timeZoneName: 'short' } : {}),
    }))
  }
  return formatters.get(key)
}

export function partsIn(ms, timeZone, withName = false) {
  const out = {}
  for (const part of formatter(timeZone, withName).formatToParts(new Date(ms))) out[part.type] = part.value
  return out
}

export function zoneName(timeZone) {
  try { return partsIn(Date.UTC(2026, 0, 1), timeZone, true).timeZoneName ?? timeZone } catch { return timeZone }
}

const numericFormatters = new Map()
function numericParts(ms, timeZone) {
  if (!numericFormatters.has(timeZone)) {
    numericFormatters.set(timeZone, new Intl.DateTimeFormat('en-US', {
      timeZone, month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric', hourCycle: 'h23',
    }))
  }
  const out = {}
  for (const part of numericFormatters.get(timeZone).formatToParts(new Date(ms))) out[part.type] = part.value
  return out
}

// 'year' | 'month' | 'day' | 'datetime' | 'datetime_seconds' | 'full' for the values of one column. Decided from all
// values, and only when there are at least two distinct ones: one point says nothing about a column's granularity.
export function granularityOf(instants, timeZone, kind) {
  const distinct = [...new Set(instants)]
  if (distinct.length < 2) return kind === 'date' ? 'day' : 'full'
  const parts = distinct.map(ms => numericParts(ms, timeZone))
  const midnight = parts.every(p => +p.hour % 24 === 0 && +p.minute === 0 && +p.second === 0)
  if (!midnight) return parts.some(p => +p.second !== 0) ? 'datetime_seconds' : 'datetime'
  if (parts.every(p => p.day === '1' && p.month === '1')) return 'year'
  if (parts.every(p => p.day === '1')) return 'month'
  return 'day'
}

export function formatInstant(ms, timeZone, granularity, { zone = false } = {}) {
  const p = partsIn(ms, timeZone, zone)
  const date = `${p.day} ${p.month} ${p.year}`
  if (granularity === 'year') return p.year
  if (granularity === 'month') return `${p.month} ${p.year}`
  if (granularity === 'day') return date
  const secs = granularity === 'datetime_seconds' || (granularity === 'full' && p.second !== '00')
  const text = `${date}, ${p.hour}:${p.minute}${secs ? `:${p.second}` : ''}`
  return zone && p.timeZoneName ? `${text} ${p.timeZoneName}` : text
}
