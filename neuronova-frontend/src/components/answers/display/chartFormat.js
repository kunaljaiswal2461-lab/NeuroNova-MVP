// Chart helpers (SPEC-10 W5.9): ticks follow the column's granularity, tooltips show the formatted value and, when it
// differs, the exact string.
import { axisTick } from './axisTick'
import { columnProfile } from './columnProfile'
import { formatValue } from './formatValue'
import { FALLBACK_TIME_ZONE } from './locale'

export function columnFormat(result, label, timeZone = FALLBACK_TIME_ZONE) {
  const columns = Array.isArray(result?.columns) ? result.columns : []
  const position = columns.findIndex(column => column.alias === label)
  const profile = columnProfile(result, position, timeZone)
  return {
    profile,
    kind: profile.kind,
    tick: value => axisTick(value, profile.kind, profile, { timeZone }),
    value: exact => formatValue(exact, profile.kind, { surface: 'tooltip', profile, timeZone }),
    // "12,34,567.50", or "≈ 3.33 (exact: 3.3333…)" when formatting changed what the eye sees.
    line: exact => {
      const shown = formatValue(exact, profile.kind, { surface: 'tooltip', profile, timeZone })
      return shown.text === exact ? exact : `${shown.text} (exact: ${exact})`
    },
  }
}
