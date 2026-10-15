// Axis ticks for charts only. Numeric ticks are approximate by nature (they label an already-plotted number);
// tooltips stay exact through formatValue (SPEC-10 W4.5).
import { DISPLAY_LOCALE, FALLBACK_TIME_ZONE } from './locale'
import { formatValue } from './formatValue'

const compact = new Intl.NumberFormat(DISPLAY_LOCALE, { notation: 'compact', maximumFractionDigits: 1 })

export function axisTick(value, kind, profile, { timeZone = FALLBACK_TIME_ZONE } = {}) {
  if (kind === 'integer' || kind === 'decimal' || kind === 'number') {
    // Recharts hands a numeric axis its plotted numbers; anything else is shown as text, never parsed.
    return typeof value === 'number' && Number.isFinite(value) ? compact.format(value) : String(value)
  }
  if (kind === 'date' || kind === 'timestamp' || kind === 'timestamptz') {
    return formatValue(String(value), kind, { surface: 'table', profile, timeZone }).text
  }
  const text = String(value ?? '')
  return text.length > 24 ? `${text.slice(0, 23)}…` : text
}
