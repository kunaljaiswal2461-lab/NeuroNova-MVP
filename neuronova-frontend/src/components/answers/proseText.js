import { formatValue } from './display/formatValue'
import { columnProfile } from './display/columnProfile'
import { resultView } from './format'

// Resolved prose from server segments (SPEC-10 W5.2). A value is formatted for display and keeps its exact string
// on hover and focus. Plain text only: nothing here sets innerHTML.
export function formatSegments(segments, result, timeZone) {
  const { index } = resultView(result)
  return segments.map(segment => {
    if (segment.t !== 'value') return { text: segment.text ?? '', value: false }
    const position = segment.column ? index(segment.column) : -1
    const profile = position >= 0 ? columnProfile(result, position, timeZone) : null
    const formatted = formatValue(segment.exact, segment.kind, { surface: 'prose', profile, timeZone })
    return { text: formatted.text, title: formatted.title, value: true }
  })
}

export const segmentsToText = formatted => formatted.map(item => item.text).join('')
