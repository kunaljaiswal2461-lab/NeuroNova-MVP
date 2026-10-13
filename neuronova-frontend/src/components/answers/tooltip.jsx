export function ExactTooltipBox({ title, lines }) {
  return (
    <div style={{ maxWidth: 300, padding: '8px 10px', border: '1px solid var(--color-border)', borderRadius: 6, background: 'var(--color-surface, white)', boxShadow: 'var(--shadow-card)' }}>
      {title != null && <strong style={{ display: 'block', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)', overflowWrap: 'anywhere' }}>{title}</strong>}
      {lines.map((line, index) => (
        <span key={index} style={{ display: 'block', marginTop: 3, fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-secondary)', overflowWrap: 'anywhere' }}>{line}</span>
      ))}
    </div>
  )
}
