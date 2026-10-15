// formatValue(exact, kind, context) -> { text, title, approximate, empty } (SPEC-10 D4).
// Formatting never changes which value is shown: `title` always carries the exact value.
import { formatDecimalText, groupInteger, parseNumber } from './numbers'
import { FALLBACK_TIME_ZONE, MAX_TABLE_TEXT } from './locale'
import { formatInstant, parseTemporal } from './temporal'

const NUMERIC = new Set(['integer', 'decimal', 'number'])
const NON_FINITE = { NaN: 'NaN', Infinity: '∞', '+Infinity': '∞', '-Infinity': '−∞' }

function numeric(exact, kind, surface) {
  if (Object.hasOwn(NON_FINITE, exact)) return { text: NON_FINITE[exact], approximate: false }
  const parsed = parseNumber(exact)
  if (!parsed || parsed.exponent !== null) return { text: String(exact), approximate: false }
  if (kind === 'integer') return { text: `${parsed.sign}${groupInteger(parsed.int)}`, approximate: false }
  const prose = surface === 'prose' || surface === 'kpi'
  const { text, approximate } = formatDecimalText(exact, prose ? 'round' : 'stripOnly')
  // A table keeps a fraction of up to four digits exactly as returned (trailing zeros included).
  if (!prose && parsed.frac.length <= 4) return { text: `${parsed.sign}${groupInteger(parsed.int)}${parsed.frac ? `.${parsed.frac}` : ''}`, approximate: false }
  return { text: approximate ? `≈ ${text}` : text, approximate }
}

export function utcTitle(exact) {
  return /Z$/i.test(exact) ? `${exact.replace('T', ' ').replace(/Z$/i, '')} UTC` : exact
}

export function formatValue(exact, kind, context = {}) {
  const { surface = 'prose', profile = null, timeZone = FALLBACK_TIME_ZONE } = context
  if (exact == null || kind === 'null' || (exact === '—' && kind == null)) {
    return { text: '—', title: 'No value', approximate: false, empty: false }
  }
  const raw = String(exact)
  if (raw === '') return { text: '(empty)', title: 'Empty text', approximate: false, empty: true }
  const base = { title: raw, approximate: false, empty: false }

  if (kind === 'percent') {
    const inner = numeric(raw.replace(/%$/, ''), 'decimal', surface)
    return { ...base, text: `${inner.text}%`, approximate: inner.approximate }
  }
  if (NUMERIC.has(kind)) return { ...base, ...numeric(raw, kind, surface) }
  if (kind === 'boolean') {
    const yes = /^(true|t|yes)$/i.test(raw)
    return { ...base, text: surface === 'table' ? (yes ? 'true' : 'false') : (yes ? 'Yes' : 'No') }
  }
  if (kind === 'uuid') return { ...base, text: raw.length > 8 ? `${raw.slice(0, 8)}…` : raw }
  if (kind === 'date' || kind === 'timestamp' || kind === 'timestamptz') {
    const parsed = parseTemporal(raw, kind)
    if (!parsed) return { ...base, text: raw }
    const zoned = kind === 'timestamptz'
    const zone = zoned ? timeZone : 'UTC'
    const granularity = profile?.granularity ?? (kind === 'date' ? 'day' : 'full')
    const showZone = zoned && surface !== 'table' && !['year', 'month', 'day'].includes(granularity)
    return {
      ...base,
      text: formatInstant(parsed.ms, zone, granularity, { zone: showZone }),
      title: zoned ? utcTitle(raw) : raw,
    }
  }
  // text and anything unknown
  if (surface === 'table' && raw.length > MAX_TABLE_TEXT) return { ...base, text: `${raw.slice(0, MAX_TABLE_TEXT - 1)}…` }
  return { ...base, text: raw }
}
