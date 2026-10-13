import { Component, useState } from 'react'
import { downloadCsv } from './csv'
import { RENDERERS } from './renderers'
import SqlPanel from './SqlPanel'
import TableRenderer from './renderers/Table'

// Answer text, cautions, the chosen display, the exact table, assumptions, the SQL, a CSV and a footer.
// Everything is rendered as plain text: nothing in this folder sets innerHTML (SPEC-09 I7, tested).

const VISUAL_DISPLAYS = new Set([
  'kpi', 'kpi_comparison', 'ranked_list', 'bar', 'grouped_bar', 'line', 'multi_line', 'area', 'pie', 'scatter',
  'histogram', 'heatmap', 'pivot',
])

class RenderBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { failed: false }
  }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch() {
    this.props.onFailure?.()
  }

  render() {
    return this.state.failed ? null : this.props.children
  }
}

const amber = { margin: 0, padding: '8px 12px', fontFamily: 'var(--font-body)', fontSize: 12, color: '#92400E', background: '#FFFBEB', border: '1px solid #FDE68A', borderRadius: 6 }
const chip = { display: 'inline-block', padding: '2px 8px', borderRadius: 999, border: '1px solid var(--color-border)', background: 'var(--color-base)', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-secondary)' }

function Footer({ result, route }) {
  const partial = result.complete === false
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>
      <span>{result.returned_row_count ?? (result.rows ?? []).length} rows{partial ? ' · partial' : ' · complete'}</span>
      {result.duration_ms !== undefined && <span>{result.duration_ms}ms</span>}
      <span>{route === 'metric' ? 'Confirmed metric' : 'Generated SQL'}</span>
    </div>
  )
}

export default function AnswerView({ result, answer }) {
  const presentation = answer?.presentation ?? null
  const display = presentation?.display ?? 'table'
  const renderer = RENDERERS[display] ?? RENDERERS.table
  const verdict = renderer.eligible(result, presentation ?? { display })
  const visual = VISUAL_DISPLAYS.has(display)
  const [drawFailed, setDrawFailed] = useState(false)
  const canDraw = visual && verdict.ok && !drawFailed
  // table_only: a table answer. chart_table: a drawable chart with the table behind a tab. table_with_note: a chart
  // that cannot be drawn, so the table is shown with a note. text_table: narrative or takeaways, table behind a tab.
  const mode = display === 'table' ? 'table_only' : visual ? (canDraw ? 'chart_table' : 'table_with_note') : 'text_table'
  const [tab, setTab] = useState(mode === 'chart_table' ? 'chart' : 'none')
  const Display = renderer.Component
  const takeaways = Array.isArray(presentation?.takeaways) ? presentation.takeaways : []
  const caveats = Array.isArray(presentation?.caveats) ? presentation.caveats : []
  const assumptions = Array.isArray(answer?.assumptions) ? answer.assumptions : []
  const showTable = mode === 'table_only' || mode === 'table_with_note' || tab === 'table'
  const showChart = mode === 'chart_table' && tab === 'chart'

  return (
    <section aria-label="Answer" style={{ display: 'grid', gap: 10 }}>
      {presentation?.answer && (
        <p data-testid="answer-text" style={{ margin: 0, fontFamily: 'var(--font-body)', fontSize: 14, lineHeight: 1.6, color: 'var(--color-text-primary)', whiteSpace: 'pre-wrap' }}>{presentation.answer}</p>
      )}
      {takeaways.length > 0 && (
        <ul aria-label="Takeaways" style={{ margin: 0, paddingLeft: 20, display: 'grid', gap: 4, fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)' }}>
          {takeaways.map((item, position) => <li key={position}>{item}</li>)}
        </ul>
      )}
      {caveats.map((caveat, position) => <p key={position} role="status" style={amber}>{caveat}</p>)}

      {showChart && (
        <div aria-label="Answer display" role="tabpanel">
          <RenderBoundary onFailure={() => setDrawFailed(true)}>
            <Display result={result} spec={presentation} onFailure={() => setDrawFailed(true)} />
          </RenderBoundary>
        </div>
      )}
      {mode === 'table_with_note' && (
        <p role="note" style={{ margin: 0, fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)' }}>Shown as a table because the chart could not be drawn.</p>
      )}
      {(mode === 'chart_table' || mode === 'text_table') && (
        <div role="tablist" aria-label="Answer view" style={{ display: 'flex', gap: 6 }}>
          {mode === 'chart_table' && <button type="button" role="tab" aria-selected={tab === 'chart'} onClick={() => setTab('chart')} className="btn btn-ghost-neutral btn-sm">Chart</button>}
          <button type="button" role="tab" aria-selected={tab === 'table'} onClick={() => setTab(tab === 'table' && mode === 'text_table' ? 'none' : 'table')} className="btn btn-ghost-neutral btn-sm">Table</button>
        </div>
      )}
      {showTable && (
        <div role="tabpanel" aria-label="Answer table">
          <TableRenderer result={result} spec={presentation} />
        </div>
      )}

      {(assumptions.length > 0 || answer?.repaired) && (
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6 }}>
          <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Assumptions</span>
          <ul aria-label="Assumptions" style={{ display: 'contents', listStyle: 'none', margin: 0, padding: 0 }}>
            {assumptions.map((item, position) => <li key={position} style={chip}>{item}</li>)}
          </ul>
          {answer?.repaired && <span style={{ ...chip, borderColor: '#FDE68A', background: '#FFFBEB', color: '#92400E' }}>Corrected automatically</span>}
        </div>
      )}

      <SqlPanel sql={answer?.sql} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={() => downloadCsv(result)}>Download CSV</button>
        <Footer result={result} route={answer?.route} />
      </div>
    </section>
  )
}
