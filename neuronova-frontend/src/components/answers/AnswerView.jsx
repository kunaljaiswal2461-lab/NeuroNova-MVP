import { Component, useState } from 'react'
import { downloadCsv } from './csv'
import { RENDERERS } from './renderers'
import SqlPanel from './SqlPanel'
import TableRenderer from './renderers/Table'
import Prose from './Prose'
import { formatSegments, segmentsToText } from './proseText'
import { repairReasonLabel } from './repairLabels'
import { FALLBACK_TIME_ZONE } from './display/locale'

// Answer text, cautions, the chosen display, the exact table, assumptions, the SQL, a CSV and a footer.
// Everything is rendered as plain text: nothing in this folder sets innerHTML (SPEC-09 I7, tested).

const VISUAL_DISPLAYS = new Set([
  'kpi', 'kpi_comparison', 'ranked_list', 'bar', 'grouped_bar', 'line', 'multi_line', 'area', 'pie', 'scatter',
  'histogram', 'heatmap', 'pivot',
])
const BASIC_SUMMARY_REASONS = new Set(['spec_invalid', 'leak', 'bad_reference', 'number_check_failed', 'llm_unavailable'])

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

function Footer({ result, route, basic }) {
  const partial = result.complete === false
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>
      <span>{result.returned_row_count ?? (result.rows ?? []).length} rows{partial ? ' · partial' : ' · complete'}</span>
      {result.duration_ms !== undefined && <span>{result.duration_ms}ms</span>}
      <span>{route === 'metric' ? 'Confirmed metric' : 'Generated SQL'}</span>
      {basic && <span title="The text is the standard summary, not the model's write-up." style={{ ...chip, fontSize: 10 }}>Basic summary</span>}
    </div>
  )
}

function CopyAnswer({ build }) {
  const [state, setState] = useState('idle')
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(build())
      setState('copied')
    } catch {
      setState('failed')
    }
  }
  return (
    <>
      <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={copy}>{state === 'copied' ? 'Copied' : 'Copy answer'}</button>
      {state === 'failed' && <span role="alert" style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: '#9F1239' }}>Copy failed. Select the text and copy it by hand.</span>}
    </>
  )
}

function Tabs({ tabs, active, onSelect }) {
  const move = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const current = tabs.findIndex(([key]) => key === active)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
      : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
    onSelect(tabs[next][0])
    event.currentTarget.parentElement?.querySelectorAll('[role="tab"]')[next]?.focus()
  }
  return (
    <div role="tablist" aria-label="Answer view" style={{ display: 'flex', gap: 6 }}>
      {tabs.map(([key, label]) => (
        <button key={key} type="button" role="tab" aria-selected={active === key} tabIndex={active === key ? 0 : -1} onClick={() => onSelect(key)} onKeyDown={move} className="btn btn-ghost-neutral btn-sm">{label}</button>
      ))}
    </div>
  )
}

export default function AnswerView({ result, answer, text = '' }) {
  const presentation = answer?.presentation ?? null
  const timeZone = presentation?.display_time_zone || FALLBACK_TIME_ZONE
  const rows = Array.isArray(result?.rows) ? result.rows : []
  const empty = rows.length === 0
  // A turn that lost its `presentation` frame renders like the pass-2 fallback: the saved text and a table.
  const display = presentation?.display ?? 'table'
  const renderer = RENDERERS[display] ?? RENDERERS.table
  const verdict = renderer.eligible(result, presentation ?? { display })
  const visual = VISUAL_DISPLAYS.has(display) && !empty
  const [drawFailed, setDrawFailed] = useState(false)
  const canDraw = visual && verdict.ok && !drawFailed
  // table_only: a table answer. chart_table: a drawable chart with the table behind a tab. table_with_note: the chart
  // threw while drawing. text_table: narrative or takeaways, table behind a tab. An ineligible chart is just a table:
  // the server already explained the downgrade in a caveat.
  const mode = display === 'table' || empty ? 'table_only'
    : visual ? (canDraw ? 'chart_table' : 'table_only') : 'text_table'
  const [tab, setTab] = useState(mode === 'chart_table' ? 'chart' : 'none')
  const Display = renderer.Component
  const answerText = presentation?.answer ?? text
  const answerSegments = presentation?.answer_segments
  const takeaways = Array.isArray(presentation?.takeaways) ? presentation.takeaways : []
  const takeawaySegments = Array.isArray(presentation?.takeaways_segments) ? presentation.takeaways_segments : []
  const caveats = [...new Set(Array.isArray(presentation?.caveats) ? presentation.caveats : [])]
  const assumptions = Array.isArray(answer?.assumptions) ? answer.assumptions : []
  const showTable = mode === 'table_only' || tab === 'table'
  const showChart = mode === 'chart_table' && tab === 'chart'
  const specForDisplay = presentation ? { ...presentation, display_time_zone: timeZone } : { display }
  const basic = BASIC_SUMMARY_REASONS.has(presentation?.fallback) || (!presentation && Boolean(text))

  const buildCopy = () => {
    const prose = Array.isArray(answerSegments) && answerSegments.length
      ? segmentsToText(formatSegments(answerSegments, result, timeZone)) : answerText
    const lines = [prose]
    takeaways.forEach((item, position) => {
      const segs = takeawaySegments[position]
      lines.push(`- ${Array.isArray(segs) && segs.length ? segmentsToText(formatSegments(segs, result, timeZone)) : item}`)
    })
    caveats.forEach(item => lines.push(`Note: ${item}`))
    return lines.filter(Boolean).join('\n')
  }

  return (
    <section aria-label="Answer" style={{ display: 'grid', gap: 10 }}>
      {answerText && (
        <Prose as="p" data-testid="answer-text" segments={answerSegments} fallback={answerText} result={result} timeZone={timeZone}
          style={{ margin: 0, fontFamily: 'var(--font-body)', fontSize: 14, lineHeight: 1.6, color: 'var(--color-text-primary)', whiteSpace: 'pre-wrap' }} />
      )}
      {takeaways.length > 0 && (
        <ul aria-label="Takeaways" style={{ margin: 0, paddingLeft: 20, display: 'grid', gap: 4, fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)' }}>
          {takeaways.map((item, position) => (
            <Prose key={position} as="li" segments={takeawaySegments[position]} fallback={item} result={result} timeZone={timeZone} />
          ))}
        </ul>
      )}
      {caveats.map((caveat, position) => <p key={position} role="status" style={amber}>{caveat}</p>)}

      {empty && (
        <p style={{ margin: 0, fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)' }}>
          {result?.complete === false
            ? 'No result rows could be safely displayed before a safety limit was reached. This does not mean no rows matched.'
            : 'No rows matched this query.'}
        </p>
      )}
      {showChart && (
        <div aria-label="Answer display" role="tabpanel">
          <RenderBoundary onFailure={() => setDrawFailed(true)}>
            <Display result={result} spec={specForDisplay} onFailure={() => setDrawFailed(true)} />
          </RenderBoundary>
        </div>
      )}
      {drawFailed && (
        <p role="note" style={{ margin: 0, fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)' }}>Shown as a table because the chart could not be drawn.</p>
      )}
      {mode === 'chart_table' && <Tabs tabs={[['chart', 'Chart'], ['table', 'Table']]} active={tab} onSelect={setTab} />}
      {mode === 'text_table' && <Tabs tabs={[['table', 'Table']]} active={tab} onSelect={key => setTab(tab === key ? 'none' : key)} />}
      {showTable && !empty && (
        <div role="tabpanel" aria-label="Answer table">
          <TableRenderer result={result} spec={specForDisplay} />
        </div>
      )}

      {(assumptions.length > 0 || answer?.repaired) && (
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6 }}>
          {assumptions.length > 0 && <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Assumptions</span>}
          <ul aria-label="Assumptions" style={{ display: 'contents', listStyle: 'none', margin: 0, padding: 0 }}>
            {assumptions.map((item, position) => <li key={position} style={chip}>{item}</li>)}
          </ul>
          {answer?.repaired && <span style={{ ...chip, borderColor: '#FDE68A', background: '#FFFBEB', color: '#92400E' }}>Corrected automatically: {repairReasonLabel(answer?.repair?.reasonCode)}</span>}
        </div>
      )}

      <SqlPanel sql={answer?.sql} firstSql={answer?.firstSql} repair={answer?.repair} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <CopyAnswer build={buildCopy} />
        <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={() => downloadCsv(result)}>Download CSV</button>
        <Footer result={result} route={answer?.route} basic={basic} />
      </div>
    </section>
  )
}
