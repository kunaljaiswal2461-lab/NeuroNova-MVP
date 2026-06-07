import { useState, useEffect } from 'react'
import Layout from '../components/Layout'
import { api } from '../api/client'
import { useDataset } from '../context/DatasetContext'

function heatmapColor(val) {
  if (val === null || val === undefined) return '#F3F4F6'
  const abs = Math.abs(val)
  if (abs >= 0.8) return '#1E3A5F'
  if (abs >= 0.6) return '#2D5A8E'
  if (abs >= 0.4) return '#7BA7CC'
  if (abs >= 0.2) return '#C5D9EC'
  return '#EFF6FF'
}

function textColor(val) {
  if (val === null || val === undefined) return '#9CA3AF'
  return Math.abs(val) >= 0.5 ? 'white' : '#1E3A5F'
}

function SkipPlaceholder({ column, reason }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', padding: '48px 0' }}>
      <div style={{
        borderLeft: '3px solid #E11D48',
        padding: '14px 18px',
        background: '#FFF1F2',
        borderRadius: '0 6px 6px 0',
        maxWidth: 480,
        width: '100%',
      }}>
        <div style={{
          fontFamily: 'var(--font-heading)',
          fontSize: 11,
          fontWeight: 700,
          color: '#E11D48',
          letterSpacing: '0.05em',
          textTransform: 'uppercase',
          marginBottom: 6,
        }}>
          ⊘ Chart cannot be generated
        </div>
        <div style={{
          fontFamily: 'var(--font-data)',
          fontSize: 14,
          fontWeight: 600,
          color: '#1C1917',
          marginBottom: 4,
        }}>
          {column}
        </div>
        <div style={{
          fontFamily: 'var(--font-body)',
          fontSize: 13,
          color: '#6B6560',
          lineHeight: 1.5,
        }}>
          {reason ?? 'This column could not be visualized.'}
        </div>
      </div>
    </div>
  )
}

// Per-chart-type display name + color, used by the Chart Catalogue.
const CHART_TYPE_DISPLAY = {
  HISTOGRAM:   { label: 'HISTOGRAM',   color: '#1E3A5F' },
  BAR:         { label: 'BAR CHART',   color: '#0D9488' },
  PIE:         { label: 'PIE CHART',   color: '#D97706' },
  SCATTER:     { label: 'SCATTER',     color: '#7C3AED' },
  BOXPLOT:     { label: 'BOX PLOT',    color: '#D97706' },
  HEATMAP:     { label: 'HEATMAP',     color: '#E11D48' },
  TIMESERIES:  { label: 'TIME SERIES', color: '#0D9488' },
  RADAR:       { label: 'RADAR',       color: '#1E3A5F' },
  NULL_MATRIX: { label: 'NULL MATRIX', color: '#E11D48' },
}

function fmt(v, decimals = 2) {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: decimals })
  return v
}

export default function Visualization() {
  const { activeDatasetId, activeDataset } = useDataset()
  const [activeTab, setActiveTab] = useState('Numeric')
  const [loading, setLoading] = useState(false)
  const [charts, setCharts] = useState([])
  const [skippedColumns, setSkippedColumns] = useState([])
  const [vizSummary, setVizSummary] = useState({ total: 0, rendered: 0 })
  const [activeCol, setActiveCol] = useState(null)
  const [activeBarCol, setActiveBarCol] = useState(null)
  const [error, setError] = useState(null)

  const TABS = ['Numeric', 'Categorical', 'Dataset', 'Temporal']

  useEffect(() => {
    if (!activeDatasetId) return
    setLoading(true)
    setError(null)
    api.get(`/datasets/${activeDatasetId}/viz`)
      .then(data => {
        const allCharts = data.charts ?? []
        setCharts(allCharts)
        setSkippedColumns(data.skipped_columns ?? [])
        setVizSummary({
          total: data.total_columns ?? 0,
          rendered: data.rendered_columns ?? 0,
        })
        const firstHistogram = allCharts.find(c => c.type === 'HISTOGRAM')
        if (firstHistogram) setActiveCol(firstHistogram.config?.x_col ?? firstHistogram.columns?.[0] ?? null)
        const firstBar = allCharts.find(c => c.type === 'BAR')
        if (firstBar) setActiveBarCol(firstBar.config?.x_col ?? firstBar.columns?.[0] ?? null)
        // Auto-pick the first tab that actually has charts
        const tabForType = { HISTOGRAM: 'Numeric', BOXPLOT: 'Numeric', BAR: 'Categorical', PIE: 'Categorical', HEATMAP: 'Dataset', SCATTER: 'Dataset', TIMESERIES: 'Temporal', NULL_MATRIX: 'Dataset', RADAR: 'Dataset' }
        const firstChart = allCharts[0]
        if (firstChart) setActiveTab(tabForType[firstChart.type] ?? 'Numeric')
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [activeDatasetId])

  // Derived collections by chart type
  const histogramCharts = charts.filter(c => c.type === 'HISTOGRAM')
  const barCharts       = charts.filter(c => c.type === 'BAR')
  const heatmapChart    = charts.find(c => c.type === 'HEATMAP') ?? null
  const timeseriesCharts= charts.filter(c => c.type === 'TIMESERIES')

  // Active histogram stats
  const activeHistogram = histogramCharts.find(c => c.config?.x_col === activeCol || c.columns?.[0] === activeCol) ?? null
  const stats = activeHistogram?.config?.stats ?? null

  // Active bar chart
  const activeBarChart = barCharts.find(c => c.config?.x_col === activeBarCol || c.columns?.[0] === activeBarCol) ?? null

  // Lookup map for skipped columns (by name → SkippedColumn record)
  const skippedByName = Object.fromEntries(skippedColumns.map(s => [s.column_name, s]))

  // Numeric selector options: rendered histograms first, then skipped numeric-intended columns
  const numericRenderedCols = histogramCharts.map(c => c.config?.x_col ?? c.columns?.[0]).filter(Boolean)
  const numericSkippedCols = skippedColumns
    .filter(s => s.intended_chart_type === 'HISTOGRAM' || s.intended_chart_type === 'NONE')
    .map(s => s.column_name)
  const numericOptions = [
    ...numericRenderedCols.map(c => ({ name: c, skipped: false })),
    ...numericSkippedCols.map(c => ({ name: c, skipped: true })),
  ]

  // Categorical selector options: rendered BARs first, then skipped categorical-intended
  const categoricalRenderedCols = barCharts.map(c => c.config?.x_col ?? c.columns?.[0]).filter(Boolean)
  const categoricalSkippedCols = skippedColumns
    .filter(s => s.intended_chart_type === 'BAR' || s.intended_chart_type === 'NONE')
    .map(s => s.column_name)
  const categoricalOptions = [
    ...categoricalRenderedCols.map(c => ({ name: c, skipped: false })),
    ...categoricalSkippedCols
      .filter(c => !categoricalRenderedCols.includes(c))
      .map(c => ({ name: c, skipped: true })),
  ]

  const activeColIsSkipped = activeCol != null && skippedByName[activeCol] != null
  const activeBarColIsSkipped = activeBarCol != null && skippedByName[activeBarCol] != null

  const noDataset = !activeDatasetId

  return (
    <Layout
      title="Visualization Center"
      subtitle={activeDataset?.original_name ?? 'Chart-ready intelligence from your dataset'}
      actions={
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {activeTab === 'Numeric' && numericOptions.length > 0 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 8, padding: '6px 10px' }}>
              <span style={{ fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Column</span>
              <select
                value={activeCol ?? ''}
                onChange={e => setActiveCol(e.target.value)}
                style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-primary)', border: 'none', background: 'transparent', outline: 'none', cursor: 'pointer' }}
              >
                {numericOptions.map(o => (
                  <option key={o.name} value={o.name}>
                    {o.skipped ? `⊘ ${o.name}` : o.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          {activeTab === 'Categorical' && categoricalOptions.length > 0 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 8, padding: '6px 10px' }}>
              <span style={{ fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Column</span>
              <select
                value={activeBarCol ?? ''}
                onChange={e => setActiveBarCol(e.target.value)}
                style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-primary)', border: 'none', background: 'transparent', outline: 'none', cursor: 'pointer' }}
              >
                {categoricalOptions.map(o => (
                  <option key={o.name} value={o.name}>
                    {o.skipped ? `⊘ ${o.name}` : o.name}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      }
    >
      {noDataset ? (
        <div className="card card-pad anim-fade-up" style={{ textAlign: 'center', padding: '48px 24px' }}>
          <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 18, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 8 }}>No Dataset Selected</h2>
          <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', marginBottom: 20 }}>
            Select a profiled dataset from the Upload Center to explore its visualizations.
          </p>
          <a href="/upload" className="btn btn-primary" style={{ display: 'inline-flex', justifyContent: 'center' }}>Go to Upload →</a>
        </div>
      ) : error ? (
        <div style={{ background: '#FFF1F2', border: '1px solid #FECDD3', borderRadius: 8, padding: '16px 20px', fontFamily: 'var(--font-body)', fontSize: 13, color: '#9F1239' }}>
          {error}
        </div>
      ) : (
        <>
          {skippedColumns.length > 0 && (
            <div style={{
              background: '#EFF6FF',
              border: '1px solid #BFDBFE',
              borderRadius: 8,
              padding: '10px 14px',
              marginBottom: 16,
              display: 'flex',
              alignItems: 'flex-start',
              gap: 10,
              fontFamily: 'var(--font-body)',
              fontSize: 13,
              color: '#1E40AF',
            }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1E40AF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: 2 }}>
                <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
              <div style={{ lineHeight: 1.5 }}>
                <strong>{skippedColumns.length} column{skippedColumns.length === 1 ? '' : 's'}</strong> could not be visualized due to data quality issues and {skippedColumns.length === 1 ? 'was' : 'were'} skipped:{' '}
                <span style={{ fontFamily: 'var(--font-data)' }}>
                  {skippedColumns.map(s => s.column_name).join(', ')}
                </span>
              </div>
            </div>
          )}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 280px', gap: 20, alignItems: 'start' }}>

          {/* ── Main Chart Panel ── */}
          <div className="card anim-fade-up" style={{ overflow: 'hidden' }}>
            {/* Tabs */}
            <div className="viz-tabs" style={{ padding: '0 20px' }}>
              {TABS.map(tab => (
                <button key={tab} className={`viz-tab ${activeTab === tab ? 'active' : ''}`} onClick={() => setActiveTab(tab)}>
                  {tab}
                </button>
              ))}
            </div>

            {/* Context Strip */}
            <div style={{ padding: '8px 20px', background: 'var(--color-base)', borderBottom: '1px solid var(--color-border)', display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
              {loading ? (
                <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-muted)', fontStyle: 'italic' }}>Loading charts…</span>
              ) : activeTab === 'Numeric' && activeHistogram ? (
                <>
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>
                    col: <strong style={{ color: 'var(--color-text-primary)' }}>{activeCol}</strong>
                  </span>
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>
                    semantic: <strong style={{ color: 'var(--color-success)' }}>{activeHistogram.semantic_context ?? '—'}</strong>
                  </span>
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)', marginLeft: 'auto' }}>
                    null%: <strong style={{ color: 'var(--color-text-muted)' }}>{fmt(activeHistogram.config?.null_pct, 1)}%</strong>
                  </span>
                </>
              ) : activeTab === 'Categorical' && activeBarChart ? (
                <>
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>
                    col: <strong style={{ color: 'var(--color-text-primary)' }}>{activeBarChart.config?.x_col ?? activeBarChart.columns?.[0]}</strong>
                  </span>
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>
                    cardinality: <strong style={{ color: 'var(--color-text-primary)' }}>{activeBarChart.config?.cardinality ?? '—'}</strong>
                  </span>
                </>
              ) : activeTab === 'Dataset' && heatmapChart ? (
                <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>
                  {heatmapChart.title} · Pearson correlation
                </span>
              ) : (
                <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-muted)', fontStyle: 'italic' }}>
                  {charts.length === 0 ? 'No chart data available' : 'Select a tab to explore'}
                </span>
              )}
            </div>

            {/* Chart Area */}
            <div style={{ padding: '20px 20px 10px' }}>

              {/* ── Big "what am I looking at?" header — visible on every tab ── */}
              {!loading && (() => {
                let columnLabel = null
                let typeLabel = null
                if (activeTab === 'Numeric') {
                  columnLabel = activeCol
                  typeLabel = activeColIsSkipped ? 'SKIPPED' : 'HISTOGRAM'
                } else if (activeTab === 'Categorical') {
                  columnLabel = activeBarCol
                  typeLabel = activeBarColIsSkipped ? 'SKIPPED' : 'BAR CHART'
                } else if (activeTab === 'Dataset') {
                  columnLabel = heatmapChart ? `${heatmapChart.config?.columns?.length ?? 0} numeric columns` : null
                  typeLabel = 'CORRELATION HEATMAP'
                } else if (activeTab === 'Temporal') {
                  columnLabel = timeseriesCharts[0]?.config?.x_col ?? null
                  typeLabel = 'TIME SERIES'
                }
                if (!columnLabel) return null
                return (
                  <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, marginBottom: 18, paddingBottom: 12, borderBottom: '1px solid var(--color-border-light)' }}>
                    <div>
                      <div style={{ fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
                        Column
                      </div>
                      <div style={{ fontFamily: 'var(--font-data)', fontSize: 20, fontWeight: 600, color: 'var(--color-text-primary)' }}>
                        {columnLabel}
                      </div>
                    </div>
                    <span style={{
                      fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700,
                      color: typeLabel === 'SKIPPED' ? 'var(--color-text-muted)' : 'var(--color-primary-indigo)',
                      background: typeLabel === 'SKIPPED' ? 'var(--color-base)' : '#EDEAFF',
                      border: `1px solid ${typeLabel === 'SKIPPED' ? 'var(--color-border)' : '#DDD9F5'}`,
                      borderRadius: 'var(--radius-full)',
                      padding: '4px 12px',
                      textTransform: 'uppercase',
                      letterSpacing: '0.06em',
                      whiteSpace: 'nowrap',
                    }}>
                      {typeLabel}
                    </span>
                  </div>
                )
              })()}

              {/* ── NUMERIC: Percentile Box ── */}
              {activeTab === 'Numeric' && activeColIsSkipped ? (
                <SkipPlaceholder column={activeCol} reason={skippedByName[activeCol]?.reason} />
              ) : activeTab === 'Numeric' && (
                !activeHistogram || !stats ? (
                  <div style={{ height: 220, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
                    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--color-border)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                      <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>
                    </svg>
                    <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>{loading ? 'Loading…' : 'No numeric charts'}</p>
                    {!loading && <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', opacity: 0.7 }}>Profile a dataset with numeric columns</p>}
                  </div>
                ) : (() => {
                  const { min, p25, median, p75, max, mean } = stats
                  const range = (max ?? 0) - (min ?? 0)
                  const toPos = (v) => range > 0 ? ((v - min) / range) * 100 : 50
                  return (
                    <div style={{ height: 220, display: 'flex', flexDirection: 'column', justifyContent: 'center', padding: '0 24px' }}>
                      <p style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 24 }}>
                        Distribution Summary — {activeCol}
                      </p>
                      {/* Box plot visualization */}
                      <div style={{ position: 'relative', height: 60, marginBottom: 16 }}>
                        {/* Whisker line */}
                        <div style={{ position: 'absolute', top: '50%', left: `${toPos(min ?? 0)}%`, right: `${100 - toPos(max ?? 0)}%`, height: 2, background: 'var(--color-primary-indigo)', transform: 'translateY(-50%)' }} />
                        {/* IQR box */}
                        <div style={{ position: 'absolute', top: '20%', left: `${toPos(p25 ?? 0)}%`, width: `${toPos(p75 ?? 0) - toPos(p25 ?? 0)}%`, height: '60%', background: 'var(--color-primary-indigo)', opacity: 0.2, borderRadius: 4, border: '1.5px solid var(--color-primary-indigo)' }} />
                        {/* Median line */}
                        <div style={{ position: 'absolute', top: '15%', left: `${toPos(median ?? 0)}%`, width: 2, height: '70%', background: 'var(--color-primary-indigo)', transform: 'translateX(-50%)' }} />
                        {/* Mean marker */}
                        {mean !== null && mean !== undefined && (
                          <div style={{ position: 'absolute', top: '-4px', left: `${toPos(mean)}%`, width: 10, height: 10, borderRadius: '50%', background: '#EF4444', transform: 'translateX(-50%)', border: '2px solid white', boxShadow: '0 0 0 1px #EF4444' }} title={`Mean: ${fmt(mean)}`} />
                        )}
                        {/* Min / Max labels */}
                        <div style={{ position: 'absolute', bottom: -20, left: `${toPos(min ?? 0)}%`, fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', transform: 'translateX(-50%)' }}>{fmt(min)}</div>
                        <div style={{ position: 'absolute', bottom: -20, left: `${toPos(max ?? 0)}%`, fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', transform: 'translateX(-50%)' }}>{fmt(max)}</div>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'center', gap: 20, marginTop: 24, flexWrap: 'wrap' }}>
                        {[['P25', p25], ['Median', median], ['Mean', mean], ['P75', p75]].map(([l, v]) => (
                          <div key={l} style={{ textAlign: 'center' }}>
                            <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', marginBottom: 2 }}>{l}</div>
                            <div style={{ fontFamily: 'var(--font-display)', fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>{fmt(v)}</div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )
                })()
              )}

              {/* ── CATEGORICAL: Bar chart ── */}
              {activeTab === 'Categorical' && activeBarColIsSkipped ? (
                <SkipPlaceholder column={activeBarCol} reason={skippedByName[activeBarCol]?.reason} />
              ) : activeTab === 'Categorical' && (
                <div>
                  <h4 style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 16 }}>
                    {activeBarChart ? activeBarChart.title : 'Category Distribution'}
                  </h4>
                  {!activeBarChart ? (
                    <div style={{ textAlign: 'center', padding: '32px 0' }}>
                      <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>{loading ? 'Loading…' : 'No categorical charts'}</p>
                      {!loading && <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', opacity: 0.7, marginTop: 4 }}>Profile a dataset with categorical columns to see distributions</p>}
                    </div>
                  ) : (() => {
                    const topValues = activeBarChart.config?.top_values ?? []
                    const total = activeBarChart.config?.total_count ?? topValues.reduce((s, [, c]) => s + c, 0)
                    const maxCount = Math.max(...topValues.map(([, c]) => c), 1)
                    return topValues.slice(0, 12).map(([label, count], i) => (
                      <div key={i} style={{ marginBottom: 10 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                          <span style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)' }}>{String(label)}</span>
                          <div style={{ display: 'flex', gap: 12 }}>
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>{count.toLocaleString()}</span>
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-muted)', width: 36, textAlign: 'right' }}>{total > 0 ? ((count / total) * 100).toFixed(1) : 0}%</span>
                          </div>
                        </div>
                        <div style={{ height: 8, background: 'var(--color-border-light)', borderRadius: 4, overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${(count / maxCount) * 100}%`, background: 'var(--color-primary)', borderRadius: 4, transition: 'width 600ms ease' }} />
                        </div>
                      </div>
                    ))
                  })()}

                  {/* Categorical column switcher */}
                  {barCharts.length > 1 && (
                    <div style={{ marginTop: 16, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {barCharts.slice(0, 8).map((c, i) => {
                        const colName = c.config?.x_col ?? c.columns?.[0]
                        return (
                          <button key={i}
                            onClick={() => setActiveBarCol(colName)}
                            style={{ padding: '4px 10px', borderRadius: 6, border: '1px solid var(--color-border)', background: c === activeBarChart ? 'var(--color-base)' : 'white', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
                            {colName}
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>
              )}

              {/* ── DATASET: Heatmap ── */}
              {activeTab === 'Dataset' && (
                <div>
                  <h4 style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 14 }}>
                    Correlation Heatmap (Pearson)
                  </h4>
                  {!heatmapChart ? (
                    <div style={{ textAlign: 'center', padding: '32px 0' }}>
                      <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>{loading ? 'Loading…' : 'No correlation data'}</p>
                      {!loading && <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', opacity: 0.7, marginTop: 4 }}>Requires ≥ 3 numeric columns</p>}
                    </div>
                  ) : (() => {
                    const cols = heatmapChart.config?.columns ?? []
                    const matrix = heatmapChart.config?.matrix ?? {}
                    const showValues = heatmapChart.config?.show_values ?? cols.length <= 10
                    return (
                      <div style={{ overflowX: 'auto' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: `auto repeat(${cols.length}, 1fr)`, gap: 2, minWidth: cols.length * 48 }}>
                          <div />
                          {cols.map(c => (
                            <div key={c} style={{ fontFamily: 'var(--font-data)', fontSize: 9, color: 'var(--color-text-muted)', textAlign: 'center', padding: '0 0 4px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c}>{c}</div>
                          ))}
                          {cols.map(rowCol => (
                            <>
                              <div key={`lbl-${rowCol}`} style={{ fontFamily: 'var(--font-data)', fontSize: 9, color: 'var(--color-text-muted)', display: 'flex', alignItems: 'center', paddingRight: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={rowCol}>{rowCol}</div>
                              {cols.map(colCol => {
                                const val = matrix[rowCol]?.[colCol] ?? null
                                return (
                                  <div key={`${rowCol}-${colCol}`}
                                    style={{ background: heatmapColor(val), borderRadius: 3, padding: '8px 2px', textAlign: 'center', fontFamily: 'var(--font-data)', fontSize: 10, color: textColor(val), fontWeight: 500, cursor: 'default' }}
                                    title={`${rowCol} × ${colCol}: ${val !== null ? val.toFixed(3) : 'N/A'}`}>
                                    {showValues ? (val !== null ? val.toFixed(2) : '—') : ''}
                                  </div>
                                )
                              })}
                            </>
                          ))}
                        </div>
                      </div>
                    )
                  })()}
                </div>
              )}

              {/* ── TEMPORAL ── */}
              {activeTab === 'Temporal' && (
                <div style={{ height: 200, position: 'relative' }}>
                  <h4 style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 14 }}>
                    {timeseriesCharts.length > 0 ? timeseriesCharts[0].title : 'Time Series'}
                  </h4>
                  {timeseriesCharts.length === 0 ? (
                    <div style={{ textAlign: 'center', padding: '32px 0' }}>
                      <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>{loading ? 'Loading…' : 'No temporal columns detected'}</p>
                      {!loading && <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', opacity: 0.7, marginTop: 4 }}>Profile a dataset with datetime columns to see trends</p>}
                    </div>
                  ) : (
                    <>
                      <svg width="100%" height="120" viewBox="0 0 600 120" preserveAspectRatio="none">
                        <defs>
                          <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor="#1E3A5F" stopOpacity="0.15" />
                            <stop offset="100%" stopColor="#1E3A5F" stopOpacity="0.01" />
                          </linearGradient>
                        </defs>
                        <path d="M0,90 C100,80 200,60 300,50 C400,40 500,30 600,15" fill="none" stroke="var(--color-primary)" strokeWidth="2.5" strokeLinecap="round" />
                        <path d="M0,90 C100,80 200,60 300,50 C400,40 500,30 600,15 L600,120 L0,120 Z" fill="url(#areaGrad)" />
                      </svg>
                      <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', marginTop: 8, textAlign: 'center' }}>
                        {timeseriesCharts[0].columns?.join(', ')} · {timeseriesCharts.length} time series chart{timeseriesCharts.length > 1 ? 's' : ''} detected
                      </p>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* ── Right Panel ── */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

            {/* Distribution Metrics */}
            <div className="card card-pad anim-fade-up" style={{ animationDelay: '60ms' }}>
              <p className="text-label-caps" style={{ color: 'var(--color-text-muted)', marginBottom: 14 }}>Distribution Metrics</p>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                {[
                  { label: 'Mean',    val: fmt(stats?.mean) },
                  { label: 'Median',  val: fmt(stats?.median) },
                  { label: 'Std Dev', val: fmt(stats?.std) },
                  { label: 'Outliers',val: activeHistogram?.config?.is_skewed ? '⚠ Skewed' : '—' },
                ].map((m, i) => (
                  <div key={i}>
                    <div style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 3 }}>{m.label}</div>
                    <div style={{ fontFamily: 'var(--font-display)', fontSize: 20, fontWeight: 700, color: stats ? 'var(--color-text-primary)' : 'var(--color-border)' }}>{m.val}</div>
                  </div>
                ))}
              </div>
              <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--color-border-light)' }}>
                {[
                  ['Min',       fmt(stats?.min)],
                  ['Max',       fmt(stats?.max)],
                  ['Skewness',  fmt(stats?.skew, 3)],
                ].map(([k, v]) => (
                  <div key={k} style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                    <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>{k}</span>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: stats ? 'var(--color-text-secondary)' : 'var(--color-text-muted)' }}>{v}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Chart Catalogue */}
            <div className="card anim-fade-up" style={{ animationDelay: '120ms' }}>
              <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--color-border)' }}>
                <p className="text-label-caps" style={{ color: 'var(--color-text-muted)' }}>Chart Catalogue</p>
              </div>
              <div style={{ padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 6 }}>
                {charts.length === 0 && skippedColumns.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: '16px 0' }}>
                    <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)' }}>{loading ? 'Loading…' : 'No charts generated'}</p>
                  </div>
                ) : (
                  <>
                    {charts.map((c, i) => {
                      const display = CHART_TYPE_DISPLAY[c.type] ?? { label: c.type, color: '#6B7280' }
                      return (
                        <div key={i} style={{ background: 'var(--color-base)', borderRadius: 6, padding: '8px 10px', cursor: 'pointer', transition: 'opacity 150ms' }}
                          onClick={() => {
                            const tab = { HISTOGRAM: 'Numeric', BOXPLOT: 'Numeric', BAR: 'Categorical', PIE: 'Categorical', HEATMAP: 'Dataset', SCATTER: 'Dataset', TIMESERIES: 'Temporal', NULL_MATRIX: 'Dataset', RADAR: 'Dataset' }[c.type] ?? 'Numeric'
                            if (tab) setActiveTab(tab)
                            if (c.type === 'BAR' || c.type === 'PIE') {
                              if (c.config?.x_col) setActiveBarCol(c.config.x_col)
                            } else if (c.config?.x_col) {
                              setActiveCol(c.config.x_col)
                            }
                          }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: display.color, textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 700 }}>{display.label}</span>
                          </div>
                          <div style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-primary)', fontWeight: 500 }}>{c.title}</div>
                        </div>
                      )
                    })}
                    {skippedColumns.length > 0 && (
                      <>
                        <div style={{ height: 1, background: 'var(--color-border-light)', margin: '6px 0' }} />
                        {skippedColumns.map((sc, i) => (
                          <button key={`sk-${i}`}
                            onClick={() => {
                              if (sc.intended_chart_type === 'BAR') {
                                setActiveTab('Categorical')
                                setActiveBarCol(sc.column_name)
                              } else {
                                setActiveTab('Numeric')
                                setActiveCol(sc.column_name)
                              }
                            }}
                            style={{
                              borderLeft: '3px solid #E11D48',
                              background: '#FFF1F2',
                              borderRadius: '0 6px 6px 0',
                              border: 'none',
                              borderLeftWidth: 3,
                              borderLeftStyle: 'solid',
                              borderLeftColor: '#E11D48',
                              padding: '8px 10px',
                              textAlign: 'left',
                              cursor: 'pointer',
                              width: '100%',
                            }}>
                            <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: '#E11D48', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700, marginBottom: 3 }}>
                              ⊘ Chart cannot be generated
                            </div>
                            <div style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: '#1C1917', fontWeight: 500, marginBottom: 2 }}>
                              {sc.column_name}
                            </div>
                            <div style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: '#6B6560', lineHeight: 1.4 }}>
                              {sc.reason}
                            </div>
                          </button>
                        ))}
                      </>
                    )}
                  </>
                )}
              </div>
            </div>

            {/* Percentiles */}
            <div className="card card-pad anim-fade-up" style={{ animationDelay: '180ms' }}>
              <p className="text-label-caps" style={{ color: 'var(--color-text-muted)', marginBottom: 12 }}>Percentiles</p>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {[['P25', stats?.p25], ['P50 (Median)', stats?.median], ['P75', stats?.p75]].map(([p, v]) => (
                  <div key={p} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>{p}</span>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: stats ? 'var(--color-text-secondary)' : 'var(--color-text-muted)' }}>{fmt(v)}</span>
                  </div>
                ))}
              </div>
            </div>

          </div>
        </div>
        </>
      )}
    </Layout>
  )
}
