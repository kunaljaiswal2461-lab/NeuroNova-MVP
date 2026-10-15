import { formatSegments } from './proseText'

export default function Prose({ segments, fallback, result, timeZone, as: Tag = 'span', ...rest }) {
  if (!Array.isArray(segments) || segments.length === 0) return <Tag {...rest}>{fallback}</Tag>
  return (
    <Tag {...rest}>
      {formatSegments(segments, result, timeZone).map((item, position) => item.value
        ? <span key={position} className="nn-value" title={item.title} tabIndex={0} style={{ borderBottom: '1px dotted var(--color-text-muted)' }}>{item.text}</span>
        : <span key={position}>{item.text}</span>)}
    </Tag>
  )
}
