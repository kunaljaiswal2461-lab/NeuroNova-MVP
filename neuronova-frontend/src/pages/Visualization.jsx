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

function fmt(v, decimals = 2) {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: decimals })
  return v
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
        <div style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: '#E11D48', letterSpacing: '0.05em', textTransform: 'uppercase', marginBottom: 6 }}>
          ⊘ Chart cannot be generated
        </div>
        <div style={{ fontFamily: 'var(--font-data)', fontSize: 14, fontWeight: 600, color: '#1C1917', marginBottom: 4 }}>
          {column}
        </div>
        <div style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: '#6B6560', lineHeight: 1.5 }}>
          {reason ?? 'This column could not be visualized.'}
        </div>
      </div>
    </div>
  )
}

// ── Small legend chip row, reused across chart types ──
function LegendRow({ items }) {
  return (
    <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', justifyContent: 'center', marginTop: 10 }}>
      {items.map(({ label, swatch, kind }) => (
        <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          {kind === 'line' ? (
            <div style={{ width: 14, height: 2, background: swatch }} />
          ) : kind === 'dot' ? (
            <div style={{ width: 8, height: 8, borderRadius: '50%', background: swatch, border: '1.5px solid white', boxShadow: `0 0 0 1px ${swatch}` }} />
          ) : (
            <div style={{ width: 12, height: 10, background: swatch, opacity: 0.4, border: `1.5px solid ${swatch}`, borderRadius: 2 }} />
          )}
          <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-secondary)' }}>{label}</span>
        </div>
      ))}
    </div>
  )
}

// ── Histogram ──
function HistogramBars({ edges, counts, meanVal, medianVal, xLabel, yLabel = 'Frequency', compact = false }) {
  if (!edges || !counts || counts.length === 0) return null
  const maxCount = Math.max(...counts, 1)
  const min = edges[0]
  const max = edges[edges.length - 1]
  const range = max - min || 1
  const toPct = (v) => ((v - min) / range) * 100
  const barHeight = compact ? 90 : 180

  return (
    <div>
      {!compact && (
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{yLabel} →</span>
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'flex-end', height: barHeight, gap: compact ? 1 : 2, position: 'relative' }}>
        {counts.map((count, i) => {
          const heightPct = (count / maxCount) * 100
          const binStart = edges[i]
          const binEnd = edges[i + 1]
          return (
            <div
              key={i}
              title={`${binStart.toFixed(2)} – ${binEnd.toFixed(2)}: ${count.toLocaleString()}`}
              style={{
                flex: 1,
                height: `${heightPct}%`,
                background: 'var(--color-primary-indigo)',
                borderRadius: '2px 2px 0 0',
                minHeight: count > 0 ? 2 : 0,
                transition: 'height 400ms ease',
                cursor: 'default',
              }}
            />
          )
        })}
        {meanVal != null && (
          <div style={{ position: 'absolute', top: 0, bottom: 0, left: `${toPct(meanVal)}%`, width: 2, background: '#EF4444', opacity: 0.8 }} title={`Mean: ${meanVal.toFixed(2)}`} />
        )}
        {medianVal != null && (
          <div style={{ position: 'absolute', top: 0, bottom: 0, left: `${toPct(medianVal)}%`, width: 2, background: 'var(--color-text-primary)', opacity: 0.5 }} title={`Median: ${medianVal.toFixed(2)}`} />
        )}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6 }}>
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{min.toFixed(2)}</span>
        {!compact && <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{xLabel} →</span>}
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{max.toFixed(2)}</span>
      </div>
      {!compact && (
        <LegendRow items={[
          { label: 'Mean', swatch: '#EF4444', kind: 'line' },
          { label: 'Median', swatch: 'var(--color-text-primary)', kind: 'line' },
        ]} />
      )}
    </div>
  )
}

// ── Box Plot ──
function BoxPlotChart({ stats, outlierPct, iqr, xLabel, compact = false }) {
  if (!stats) return null
  const { min, p25, median, p75, max, mean } = stats
  const range = (max ?? 0) - (min ?? 0)
  const toPos = (v) => range > 0 ? ((v - min) / range) * 100 : 50
  const boxHeight = compact ? 40 : 70

  return (
    <div>
      <div style={{ position: 'relative', height: boxHeight, marginBottom: compact ? 16 : 28, marginTop: compact ? 8 : 0 }}>
        <div style={{ position: 'absolute', top: '50%', left: `${toPos(min)}%`, right: `${100 - toPos(max)}%`, height: 2, background: 'var(--color-primary-indigo)', transform: 'translateY(-50%)' }} />
        <div style={{ position: 'absolute', top: '20%', left: `${toPos(p25)}%`, width: `${toPos(p75) - toPos(p25)}%`, height: '60%', background: 'var(--color-primary-indigo)', opacity: 0.2, borderRadius: 4, border: '1.5px solid var(--color-primary-indigo)' }} />
        <div style={{ position: 'absolute', top: '15%', left: `${toPos(median)}%`, width: 2, height: '70%', background: 'var(--color-primary-indigo)', transform: 'translateX(-50%)' }} />
        {mean != null && (
          <div style={{ position: 'absolute', top: -4, left: `${toPos(mean)}%`, width: 10, height: 10, borderRadius: '50%', background: '#EF4444', transform: 'translateX(-50%)', border: '2px solid white', boxShadow: '0 0 0 1px #EF4444' }} title={`Mean: ${mean.toFixed(2)}`} />
        )}
        <div style={{ position: 'absolute', bottom: -20, left: `${toPos(min)}%`, fontFamily: 'var(--font-data)', fontSize: 9, color: 'var(--color-text-muted)', transform: 'translateX(-50%)' }}>{min?.toFixed(2)}</div>
        <div style={{ position: 'absolute', bottom: -20, left: `${toPos(max)}%`, fontFamily: 'var(--font-data)', fontSize: 9, color: 'var(--color-text-muted)', transform: 'translateX(-50%)' }}>{max?.toFixed(2)}</div>
      </div>
      {!compact && xLabel && (
        <p style={{ textAlign: 'center', fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 8 }}>{xLabel} →</p>
      )}
      <div style={{ display: 'flex', justifyContent: 'center', gap: 16, flexWrap: 'wrap' }}>
        {outlierPct != null && (
          <span style={{ fontFamily: 'var(--font-data)', fontSize: compact ? 10 : 12, color: outlierPct > 5 ? '#E11D48' : 'var(--color-text-muted)' }}>
            {outlierPct.toFixed(2)}% outliers
          </span>
        )}
        {iqr != null && (
          <span style={{ fontFamily: 'var(--font-data)', fontSize: compact ? 10 : 12, color: 'var(--color-text-muted)' }}>
            IQR: {iqr.toFixed(2)}
          </span>
        )}
      </div>
      {!compact && (
        <LegendRow items={[
          { label: 'Mean', swatch: '#EF4444', kind: 'dot' },
          { label: 'Median', swatch: 'var(--color-primary-indigo)', kind: 'line' },
          { label: 'IQR (25th–75th pct)', swatch: 'var(--color-primary-indigo)', kind: 'box' },
        ]} />
      )}
    </div>
  )
}

// ── Bar Chart ──
function BarChartBody({ topValues, xLabel, yLabel = 'Count', compact = false, maxRows = 12 }) {
  if (!topValues || topValues.length === 0) return null
  const total = topValues.reduce((s, [, c]) => s + c, 0)
  const maxCount = Math.max(...topValues.map(([, c]) => c), 1)
  const rows = topValues.slice(0, compact ? 6 : maxRows)
  return (
    <div>
      {!compact && (
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
          <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{xLabel}</span>
          <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{yLabel}</span>
        </div>
      )}
      {rows.map(([label, count], i) => (
        <div key={i} style={{ marginBottom: compact ? 6 : 10 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
            <span style={{ fontFamily: 'var(--font-body)', fontSize: compact ? 11 : 13, color: 'var(--color-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: compact ? 100 : 220 }}>{String(label)}</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <span style={{ fontFamily: 'var(--font-data)', fontSize: compact ? 10 : 12, color: 'var(--color-text-secondary)' }}>{count.toLocaleString()}</span>
              {!compact && <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-muted)', width: 36, textAlign: 'right' }}>{total > 0 ? ((count / total) * 100).toFixed(1) : 0}%</span>}
            </div>
          </div>
          <div style={{ height: compact ? 5 : 8, background: 'var(--color-border-light)', borderRadius: 4, overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${(count / maxCount) * 100}%`, background: 'var(--color-primary)', borderRadius: 4, transition: 'width 600ms ease' }} />
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Scatter (correlation-strength summary — no raw point pairs available yet) ──
function ScatterSummaryCard({ chart, compact = false }) {
  const cfg = chart.config ?? {}
  const r = cfg.best_r ?? cfg.pearson_r ?? 0
  const abs_r = Math.abs(r)
  const strengthColor = abs_r >= 0.85 ? '#E11D48' : abs_r >= 0.6 ? '#D97706' : 'var(--color-text-muted)'
  const barPct = Math.min(100, abs_r * 100)
  return (
    <div style={{ padding: compact ? '4px 0' : '10px 0' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8, fontFamily: 'var(--font-data)', fontSize: compact ? 11 : 13 }}>
        <span style={{ color: 'var(--color-text-primary)' }}>{cfg.x_label ?? cfg.x_col}</span>
        <span style={{ color: 'var(--color-text-muted)' }}>×</span>
        <span style={{ color: 'var(--color-text-primary)' }}>{cfg.y_label ?? cfg.y_col}</span>
      </div>
      <div style={{ height: 10, background: 'var(--color-border-light)', borderRadius: 5, overflow: 'hidden', marginBottom: 6 }}>
        <div style={{ height: '100%', width: `${barPct}%`, background: strengthColor, borderRadius: 5 }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: strengthColor, fontWeight: 700, textTransform: 'uppercase' }}>
          {cfg.correlation_strength?.replace('_', ' ') ?? '—'} · {cfg.direction ?? ''}
        </span>
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>
          r = {fmt(r, 3)}
        </span>
      </div>
      {!compact && (
        <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginTop: 10, lineHeight: 1.4 }}>
          Pearson: {fmt(cfg.pearson_r, 3)} · Spearman: {fmt(cfg.spearman_r, 3)}. Point-by-point scatter plot requires raw sampled values, not yet computed by the profiler.
        </p>
      )}
    </div>
  )
}

// ── Timeseries (range/coverage summary — no aggregated series data available yet) ──
function TimeseriesSummaryCard({ chart, compact = false }) {
  const cfg = chart.config ?? {}
  return (
    <div style={{ padding: compact ? '4px 0' : '10px 0' }}>
      <div style={{ fontFamily: 'var(--font-data)', fontSize: compact ? 11 : 13, color: 'var(--color-text-primary)', marginBottom: 8 }}>
        {cfg.x_label} spanning {cfg.range_days ? `${Math.round(cfg.range_days)} days` : '—'}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 6 }}>
        <span>{cfg.x_min ? new Date(cfg.x_min).toLocaleDateString() : '—'}</span>
        <span>→</span>
        <span>{cfg.x_max ? new Date(cfg.x_max).toLocaleDateString() : '—'}</span>
      </div>
      <div style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-secondary)' }}>
        Tracking: {(cfg.y_labels ?? cfg.y_cols ?? []).join(', ')}
      </div>
      {!compact && (
        <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginTop: 10, lineHeight: 1.4 }}>
          Suggested bucketing: {cfg.aggregate ?? 'none'}. Trend-line rendering requires aggregated time-bucket values, not yet computed by the profiler.
        </p>
      )}
    </div>
  )
}

// ── Gallery card shell — consistent title/frame for every chart type ──
function GalleryCard({ title, subtitle, children }) {
  return (
    <div className="card" style={{ padding: 16, display: 'flex', flexDirection: 'column' }}>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontFamily: 'var(--font-data)', fontSize: 14, fontWeight: 600, color: 'var(--color-text-primary)' }}>{title}</div>
        {subtitle && <div style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginTop: 1 }}>{subtitle}</div>}
      </div>
      <div style={{ flex: 1 }}>{children}</div>
    </div>
  )
}

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

const GALLERY_SECTIONS = [
  { key: 'HISTOGRAM', label: 'Histograms' },
  { key: 'BOXPLOT', label: 'Box Plots' },
  { key: 'BAR', label: 'Bar Charts' },
  { key: 'SCATTER', label: 'Scatter (Correlations)' },
  { key: 'TIMESERIES', label: 'Time Series' },
]

function NullMatrixChart({ columns, nullPcts, flagged }) {
  if (!columns || columns.length === 0) return null
  const flaggedSet = new Set(flagged ?? [])
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Column</span>
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Null %</span>
      </div>
      {columns.map((col, i) => {
        const pct = nullPcts[i] ?? 0
        const isFlagged = flaggedSet.has(col)
        return (
          <div key={col} style={{ marginBottom: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)' }}>{col}</span>
              <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: isFlagged ? '#E11D48' : 'var(--color-text-secondary)' }}>{pct.toFixed(1)}%</span>
            </div>
            <div style={{ height: 8, background: 'var(--color-border-light)', borderRadius: 4, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${pct}%`, background: isFlagged ? '#E11D48' : 'var(--color-primary)', borderRadius: 4 }} />
            </div>
          </div>
        )
      })}
      <LegendRow items={[
        { label: 'Normal', swatch: 'var(--color-primary)', kind: 'box' },
        { label: 'Flagged (high null%)', swatch: '#E11D48', kind: 'box' },
      ]} />
    </div>
  )
}

function RadarChart({ dimensions, scores, axisMax = 100 }) {
  if (!dimensions || !scores) return null
  const n = dimensions.length
  const cx = 100, cy = 100, r = 80
  const angleFor = (i) => (Math.PI * 2 * i) / n - Math.PI / 2
  const pointFor = (i, val) => {
    const scale = val / axisMax
    const angle = angleFor(i)
    return [cx + r * scale * Math.cos(angle), cy + r * scale * Math.sin(angle)]
  }
  const dataPoints = scores.map((s, i) => pointFor(i, s))
  const dataPath = dataPoints.map(p => p.join(',')).join(' ')
  const axisLines = dimensions.map((_, i) => {
    const [x, y] = pointFor(i, axisMax)
    return <line key={i} x1={cx} y1={cy} x2={x} y2={y} stroke="var(--color-border)" strokeWidth="1" />
  })
  const labels = dimensions.map((d, i) => {
    const [x, y] = pointFor(i, axisMax * 1.18)
    return (
      <text key={d} x={x} y={y} fontSize="9" fill="var(--color-text-muted)" textAnchor="middle" fontFamily="var(--font-data)">
        {d}
      </text>
    )
  })
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
      <svg viewBox="0 0 200 200" width="220" height="220">
        {[0.25, 0.5, 0.75, 1].map(f => (
          <circle key={f} cx={cx} cy={cy} r={r * f} fill="none" stroke="var(--color-border-light)" strokeWidth="1" />
        ))}
        {axisLines}
        <polygon points={dataPath} fill="var(--color-primary-indigo)" fillOpacity="0.25" stroke="var(--color-primary-indigo)" strokeWidth="2" />
        {dataPoints.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="3" fill="var(--color-primary-indigo)" />)}
        {labels}
      </svg>
      <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginTop: 4 }}>
        Each axis: 0 (center) to {axisMax} (edge). Larger shape = healthier dataset.
      </p>
    </div>
  )
}

export default function Visualization() {
  const { activeDatasetId, activeDataset } = useDataset()
  const [viewMode, setViewMode] = useState('single') // 'single' | 'gallery'
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
        setVizSummary({ total: data.total_columns ?? 0, rendered: data.rendered_columns ?? 0 })
        const firstHistogram = allCharts.find(c => c.type === 'HISTOGRAM')
        if (firstHistogram) setActiveCol(firstHistogram.config?.x_col ?? firstHistogram.columns?.[0] ?? null)
        const firstBar = allCharts.find(c => c.type === 'BAR')
        if (firstBar) setActiveBarCol(firstBar.config?.x_col ?? firstBar.columns?.[0] ?? null)
        const tabForType = { HISTOGRAM: 'Numeric', BOXPLOT: 'Numeric', BAR: 'Categorical', PIE: 'Categorical', HEATMAP: 'Dataset', SCATTER: 'Dataset', TIMESERIES: 'Temporal', NULL_MATRIX: 'Dataset', RADAR: 'Dataset' }
        const firstChart = allCharts[0]
        if (firstChart) setActiveTab(tabForType[firstChart.type] ?? 'Numeric')
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [activeDatasetId])

  const histogramCharts = charts.filter(c => c.type === 'HISTOGRAM')
  const boxplotCharts   = charts.filter(c => c.type === 'BOXPLOT')
  const barCharts       = charts.filter(c => c.type === 'BAR')
  const scatterCharts   = charts.filter(c => c.type === 'SCATTER')
  const heatmapChart    = charts.find(c => c.type === 'HEATMAP') ?? null
  const timeseriesCharts= charts.filter(c => c.type === 'TIMESERIES')

  const activeHistogram = histogramCharts.find(c => c.config?.x_col === activeCol || c.columns?.[0] === activeCol) ?? null
  const stats = activeHistogram?.config?.stats ?? null
  const activeBoxplot = boxplotCharts.find(c => c.config?.y_col === activeCol || c.columns?.[0] === activeCol) ?? null
  const activeBarChart = barCharts.find(c => c.config?.x_col === activeBarCol || c.columns?.[0] === activeBarCol) ?? null

  const skippedByName = Object.fromEntries(skippedColumns.map(s => [s.column_name, s]))

  const numericRenderedCols = histogramCharts.map(c => c.config?.x_col ?? c.columns?.[0]).filter(Boolean)
  const numericSkippedCols = skippedColumns.filter(s => s.intended_chart_type === 'HISTOGRAM' || s.intended_chart_type === 'NONE').map(s => s.column_name)
  const numericOptions = [
    ...numericRenderedCols.map(c => ({ name: c, skipped: false })),
    ...numericSkippedCols.map(c => ({ name: c, skipped: true })),
  ]

  const categoricalRenderedCols = barCharts.map(c => c.config?.x_col ?? c.columns?.[0]).filter(Boolean)
  const categoricalSkippedCols = skippedColumns.filter(s => s.intended_chart_type === 'BAR' || s.intended_chart_type === 'NONE').map(s => s.column_name)
  const categoricalOptions = [
    ...categoricalRenderedCols.map(c => ({ name: c, skipped: false })),
    ...categoricalSkippedCols.filter(c => !categoricalRenderedCols.includes(c)).map(c => ({ name: c, skipped: true })),
  ]

  const activeColIsSkipped = activeCol != null && skippedByName[activeCol] != null
  const activeBarColIsSkipped = activeBarCol != null && skippedByName[activeBarCol] != null
  const noDataset = !activeDatasetId

  const totalGalleryCharts = histogramCharts.length + boxplotCharts.length + barCharts.length + scatterCharts.length + timeseriesCharts.length

  return (
    <Layout
      title="Visualization Center"
      subtitle={activeDataset?.original_name ?? 'Chart-ready intelligence from your dataset'}
      actions={
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ display: 'flex', background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 8, padding: 3 }}>
            {['single', 'gallery'].map(mode => (
              <button key={mode} onClick={() => setViewMode(mode)} style={{
                padding: '5px 12px', borderRadius: 6, border: 'none', cursor: 'pointer',
                background: viewMode === mode ? 'white' : 'transparent',
                boxShadow: viewMode === mode ? '0 1px 2px rgba(0,0,0,0.08)' : 'none',
                fontFamily: 'var(--font-data)', fontSize: 11, fontWeight: 600,
                color: viewMode === mode ? 'var(--color-primary-indigo)' : 'var(--color-text-muted)',
                textTransform: 'uppercase', letterSpacing: '0.04em',
              }}>
                {mode === 'single' ? 'Single View' : `Gallery (${totalGalleryCharts})`}
              </button>
            ))}
          </div>
          {viewMode === 'single' && activeTab === 'Numeric' && numericOptions.length > 0 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 8, padding: '6px 10px' }}>
              <span style={{ fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Column</span>
              <select value={activeCol ?? ''} onChange={e => setActiveCol(e.target.value)} style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-primary)', border: 'none', background: 'transparent', outline: 'none', cursor: 'pointer' }}>
                {numericOptions.map(o => <option key={o.name} value={o.name}>{o.skipped ? `⊘ ${o.name}` : o.name}</option>)}
              </select>
            </div>
          )}
          {viewMode === 'single' && activeTab === 'Categorical' && categoricalOptions.length > 0 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 8, padding: '6px 10px' }}>
              <span style={{ fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Column</span>
              <select value={activeBarCol ?? ''} onChange={e => setActiveBarCol(e.target.value)} style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-primary)', border: 'none', background: 'transparent', outline: 'none', cursor: 'pointer' }}>
                {categoricalOptions.map(o => <option key={o.name} value={o.name}>{o.skipped ? `⊘ ${o.name}` : o.name}</option>)}
              </select>
            </div>
          )}
        </div>
      }
    >
      {noDataset ? (
        <div className="card card-pad anim-fade-up" style={{ textAlign: 'center', padding: '48px 24px' }}>
          <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 18, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 8 }}>No Dataset Selected</h2>
          <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', marginBottom: 20 }}>Select a profiled dataset from the Upload Center to explore its visualizations.</p>
          <a href="/upload" className="btn btn-primary" style={{ display: 'inline-flex', justifyContent: 'center' }}>Go to Upload →</a>
        </div>
      ) : error ? (
        <div style={{ background: '#FFF1F2', border: '1px solid #FECDD3', borderRadius: 8, padding: '16px 20px', fontFamily: 'var(--font-body)', fontSize: 13, color: '#9F1239' }}>{error}</div>
      ) : viewMode === 'gallery' ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 32 }}>
          {skippedColumns.length > 0 && (
            <div style={{ background: '#EFF6FF', border: '1px solid #BFDBFE', borderRadius: 8, padding: '10px 14px', display: 'flex', alignItems: 'flex-start', gap: 10, fontFamily: 'var(--font-body)', fontSize: 13, color: '#1E40AF' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1E40AF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: 2 }}>
                <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
              <div>
                <strong>{skippedColumns.length} column{skippedColumns.length === 1 ? '' : 's'}</strong> could not be visualized: <span style={{ fontFamily: 'var(--font-data)' }}>{skippedColumns.map(s => s.column_name).join(', ')}</span>
              </div>
            </div>
          )}

          {GALLERY_SECTIONS.map(section => {
            const items = charts.filter(c => c.type === section.key)
            if (items.length === 0) return null
            return (
              <div key={section.key}>
                <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 13, fontWeight: 700, color: 'var(--color-text-primary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: CHART_TYPE_DISPLAY[section.key]?.color }} />
                  {section.label}
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)', fontWeight: 400 }}>({items.length})</span>
                </h3>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 14 }}>
                  {items.map(chart => {
                    const cfg = chart.config ?? {}
                    if (section.key === 'HISTOGRAM') {
                      return (
                        <GalleryCard key={chart.chart_id} title={cfg.x_label ?? chart.columns?.[0]} subtitle={cfg.is_skewed ? `⚠ Skewed (skew=${fmt(cfg.skew_value, 2)})` : chart.semantic_context}>
                          <HistogramBars edges={cfg.bin_edges} counts={cfg.bin_counts} meanVal={cfg.stats?.mean} medianVal={cfg.stats?.median} xLabel={cfg.x_label} compact />
                        </GalleryCard>
                      )
                    }
                    if (section.key === 'BOXPLOT') {
                      return (
                        <GalleryCard key={chart.chart_id} title={cfg.y_label ?? chart.columns?.[0]} subtitle={chart.semantic_context}>
                          <BoxPlotChart stats={cfg.stats} outlierPct={cfg.outlier_pct} iqr={cfg.iqr} compact />
                        </GalleryCard>
                      )
                    }
                    if (section.key === 'BAR') {
                      return (
                        <GalleryCard key={chart.chart_id} title={cfg.x_label ?? chart.columns?.[0]} subtitle={`${cfg.cardinality ?? '—'} unique values`}>
                          <BarChartBody topValues={cfg.top_values} xLabel={cfg.x_label} yLabel={cfg.y_label} compact />
                        </GalleryCard>
                      )
                    }
                    if (section.key === 'SCATTER') {
                      return (
                        <GalleryCard key={chart.chart_id} title={chart.title}>
                          <ScatterSummaryCard chart={chart} compact />
                        </GalleryCard>
                      )
                    }
                    if (section.key === 'TIMESERIES') {
                      return (
                        <GalleryCard key={chart.chart_id} title={chart.title}>
                          <TimeseriesSummaryCard chart={chart} compact />
                        </GalleryCard>
                      )
                    }
                    return null
                  })}
                </div>
              </div>
            )
          })}

          {heatmapChart && (
            <div>
              <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 13, fontWeight: 700, color: 'var(--color-text-primary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 14 }}>
                Correlation Heatmap
              </h3>
              <div className="card" style={{ padding: 20 }}>
                {(() => {
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
                                <div key={`${rowCol}-${colCol}`} style={{ background: heatmapColor(val), borderRadius: 3, padding: '8px 2px', textAlign: 'center', fontFamily: 'var(--font-data)', fontSize: 10, color: textColor(val), fontWeight: 500 }} title={`${rowCol} × ${colCol}: ${val !== null ? val.toFixed(3) : 'N/A'}`}>
                                  {showValues ? (val !== null ? val.toFixed(2) : '—') : ''}
                                </div>
                              )
                            })}
                          </>
                        ))}
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 16, justifyContent: 'center' }}>
                        <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>−1</span>
                        <div style={{ width: 140, height: 10, borderRadius: 5, background: 'linear-gradient(90deg, #1E3A5F, #EFF6FF, #1E3A5F)' }} />
                        <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>+1</span>
                        <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginLeft: 8 }}>darker = stronger correlation (either direction)</span>
                      </div>
                    </div>
                  )
                })()}
              </div>
            </div>
          )}
        </div>
      ) : (
        <>
          {skippedColumns.length > 0 && (
            <div style={{ background: '#EFF6FF', border: '1px solid #BFDBFE', borderRadius: 8, padding: '10px 14px', marginBottom: 16, display: 'flex', alignItems: 'flex-start', gap: 10, fontFamily: 'var(--font-body)', fontSize: 13, color: '#1E40AF' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1E40AF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: 2 }}>
                <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
              <div style={{ lineHeight: 1.5 }}>
                <strong>{skippedColumns.length} column{skippedColumns.length === 1 ? '' : 's'}</strong> could not be visualized due to data quality issues and {skippedColumns.length === 1 ? 'was' : 'were'} skipped: <span style={{ fontFamily: 'var(--font-data)' }}>{skippedColumns.map(s => s.column_name).join(', ')}</span>
              </div>
            </div>
          )}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 280px', gap: 20, alignItems: 'start' }}>
            <div className="card anim-fade-up" style={{ overflow: 'hidden' }}>
              <div className="viz-tabs" style={{ padding: '0 20px' }}>
                {TABS.map(tab => (
                  <button key={tab} className={`viz-tab ${activeTab === tab ? 'active' : ''}`} onClick={() => setActiveTab(tab)}>{tab}</button>
                ))}
              </div>

              <div style={{ padding: '8px 20px', background: 'var(--color-base)', borderBottom: '1px solid var(--color-border)', display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
                {loading ? (
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-muted)', fontStyle: 'italic' }}>Loading charts…</span>
                ) : activeTab === 'Numeric' && activeHistogram ? (
                  <>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>col: <strong style={{ color: 'var(--color-text-primary)' }}>{activeCol}</strong></span>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>semantic: <strong style={{ color: 'var(--color-success)' }}>{activeHistogram.semantic_context ?? '—'}</strong></span>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)', marginLeft: 'auto' }}>null%: <strong style={{ color: 'var(--color-text-muted)' }}>{fmt(activeHistogram.config?.null_pct, 1)}%</strong></span>
                  </>
                ) : activeTab === 'Categorical' && activeBarChart ? (
                  <>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>col: <strong style={{ color: 'var(--color-text-primary)' }}>{activeBarChart.config?.x_col ?? activeBarChart.columns?.[0]}</strong></span>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>cardinality: <strong style={{ color: 'var(--color-text-primary)' }}>{activeBarChart.config?.cardinality ?? '—'}</strong></span>
                  </>
                ) : activeTab === 'Dataset' && heatmapChart ? (
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>{heatmapChart.title} · Pearson correlation</span>
                ) : (
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-muted)', fontStyle: 'italic' }}>{charts.length === 0 ? 'No chart data available' : 'Select a tab to explore'}</span>
                )}
              </div>

              <div style={{ padding: '20px 20px 10px' }}>
                {!loading && (() => {
                  let columnLabel = null, typeLabel = null
                  if (activeTab === 'Numeric') { columnLabel = activeCol; typeLabel = activeColIsSkipped ? 'SKIPPED' : 'HISTOGRAM' }
                  else if (activeTab === 'Categorical') { columnLabel = activeBarCol; typeLabel = activeBarColIsSkipped ? 'SKIPPED' : 'BAR CHART' }
                  else if (activeTab === 'Dataset') { columnLabel = heatmapChart ? `${heatmapChart.config?.columns?.length ?? 0} numeric columns` : null; typeLabel = 'CORRELATION HEATMAP' }
                  else if (activeTab === 'Temporal') { columnLabel = timeseriesCharts[0]?.config?.x_col ?? null; typeLabel = 'TIME SERIES' }
                  if (!columnLabel) return null
                  return (
                    <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, marginBottom: 18, paddingBottom: 12, borderBottom: '1px solid var(--color-border-light)' }}>
                      <div>
                        <div style={{ fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>Column</div>
                        <div style={{ fontFamily: 'var(--font-data)', fontSize: 20, fontWeight: 600, color: 'var(--color-text-primary)' }}>{columnLabel}</div>
                      </div>
                      <span style={{ fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: typeLabel === 'SKIPPED' ? 'var(--color-text-muted)' : 'var(--color-primary-indigo)', background: typeLabel === 'SKIPPED' ? 'var(--color-base)' : '#EDEAFF', border: `1px solid ${typeLabel === 'SKIPPED' ? 'var(--color-border)' : '#DDD9F5'}`, borderRadius: 'var(--radius-full)', padding: '4px 12px', textTransform: 'uppercase', letterSpacing: '0.06em', whiteSpace: 'nowrap' }}>{typeLabel}</span>
                    </div>
                  )
                })()}

                {activeTab === 'Numeric' && activeColIsSkipped ? (
                  <SkipPlaceholder column={activeCol} reason={skippedByName[activeCol]?.reason} />
                ) : activeTab === 'Numeric' && (
                  !activeHistogram || !stats ? (
                    <div style={{ height: 220, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
                      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--color-border)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                        <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>
                      </svg>
                      <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>{loading ? 'Loading…' : 'No numeric charts'}</p>
                    </div>
                  ) : (
                    <div style={{ padding: '10px 20px 20px' }}>
                      <p style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 20 }}>Distribution — {activeCol}</p>
                      <HistogramBars edges={activeHistogram.config?.bin_edges} counts={activeHistogram.config?.bin_counts} meanVal={stats.mean} medianVal={stats.median} xLabel={activeHistogram.config?.x_label ?? activeCol} yLabel={activeHistogram.config?.y_label} />
                      <div style={{ display: 'flex', justifyContent: 'center', gap: 20, marginTop: 24, flexWrap: 'wrap' }}>
                        {[['P25', stats.p25], ['Median', stats.median], ['Mean', stats.mean], ['P75', stats.p75]].map(([l, v]) => (
                          <div key={l} style={{ textAlign: 'center' }}>
                            <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', marginBottom: 2 }}>{l}</div>
                            <div style={{ fontFamily: 'var(--font-display)', fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>{fmt(v)}</div>
                          </div>
                        ))}
                      </div>
                      {activeBoxplot && (
                        <div style={{ marginTop: 32, paddingTop: 24, borderTop: '1px solid var(--color-border-light)' }}>
                          <p style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Spread &amp; Outliers — {activeCol}</p>
                          <BoxPlotChart stats={activeBoxplot.config?.stats} outlierPct={activeBoxplot.config?.outlier_pct} iqr={activeBoxplot.config?.iqr} xLabel={activeBoxplot.config?.y_label ?? activeCol} />
                        </div>
                      )}
                    </div>
                  )
                )}

                {activeTab === 'Categorical' && activeBarColIsSkipped ? (
                  <SkipPlaceholder column={activeBarCol} reason={skippedByName[activeBarCol]?.reason} />
                ) : activeTab === 'Categorical' && (
                  <div>
                    <h4 style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 16 }}>{activeBarChart ? activeBarChart.title : 'Category Distribution'}</h4>
                    {!activeBarChart ? (
                      <div style={{ textAlign: 'center', padding: '32px 0' }}>
                        <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>{loading ? 'Loading…' : 'No categorical charts'}</p>
                      </div>
                    ) : (
                      <BarChartBody topValues={activeBarChart.config?.top_values} xLabel={activeBarChart.config?.x_label ?? 'Category'} yLabel={activeBarChart.config?.y_label ?? 'Count'} maxRows={12} />
                    )}
                    {barCharts.length > 1 && (
                      <div style={{ marginTop: 16, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        {barCharts.slice(0, 8).map((c, i) => {
                          const colName = c.config?.x_col ?? c.columns?.[0]
                          return (
                            <button key={i} onClick={() => setActiveBarCol(colName)} style={{ padding: '4px 10px', borderRadius: 6, border: '1px solid var(--color-border)', background: c === activeBarChart ? 'var(--color-base)' : 'white', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-secondary)', cursor: 'pointer' }}>{colName}</button>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )}

                {activeTab === 'Dataset' && (
                  <div>
                    <h4 style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 14 }}>Correlation Heatmap (Pearson)</h4>
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
                            {cols.map(c => <div key={c} style={{ fontFamily: 'var(--font-data)', fontSize: 9, color: 'var(--color-text-muted)', textAlign: 'center', padding: '0 0 4px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c}>{c}</div>)}
                            {cols.map(rowCol => (
                              <>
                                <div key={`lbl-${rowCol}`} style={{ fontFamily: 'var(--font-data)', fontSize: 9, color: 'var(--color-text-muted)', display: 'flex', alignItems: 'center', paddingRight: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={rowCol}>{rowCol}</div>
                                {cols.map(colCol => {
                                  const val = matrix[rowCol]?.[colCol] ?? null
                                  return <div key={`${rowCol}-${colCol}`} style={{ background: heatmapColor(val), borderRadius: 3, padding: '8px 2px', textAlign: 'center', fontFamily: 'var(--font-data)', fontSize: 10, color: textColor(val), fontWeight: 500 }} title={`${rowCol} × ${colCol}: ${val !== null ? val.toFixed(3) : 'N/A'}`}>{showValues ? (val !== null ? val.toFixed(2) : '—') : ''}</div>
                                })}
                              </>
                            ))}
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 16, justifyContent: 'center' }}>
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>−1</span>
                            <div style={{ width: 140, height: 10, borderRadius: 5, background: 'linear-gradient(90deg, #1E3A5F, #EFF6FF, #1E3A5F)' }} />
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>+1</span>
                            <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginLeft: 8 }}>darker = stronger correlation</span>
                          </div>
                        </div>
                      )
                    })()}

                    {(() => {
                      const nullChart = charts.find(c => c.type === 'NULL_MATRIX')
                      if (!nullChart) return null
                      return (
                        <div style={{ marginTop: 32, paddingTop: 24, borderTop: '1px solid var(--color-border-light)' }}>
                          <h4 style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 14 }}>Null Distribution</h4>
                          <NullMatrixChart columns={nullChart.config?.columns} nullPcts={nullChart.config?.null_pcts} flagged={nullChart.config?.flagged_columns} />
                        </div>
                      )
                    })()}

                    {(() => {
                      const radarChart = charts.find(c => c.type === 'RADAR')
                      if (!radarChart) return null
                      return (
                        <div style={{ marginTop: 32, paddingTop: 24, borderTop: '1px solid var(--color-border-light)', display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                          <h4 style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 14, alignSelf: 'flex-start' }}>{radarChart.title}</h4>
                          <RadarChart dimensions={radarChart.config?.dimensions} scores={radarChart.config?.scores} axisMax={radarChart.config?.axis_max ?? 100} />
                        </div>
                      )
                    })()}
                  </div>
                )}

                {activeTab === 'Temporal' && (
                  <div>
                    <h4 style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 14 }}>{timeseriesCharts.length > 0 ? timeseriesCharts[0].title : 'Time Series'}</h4>
                    {timeseriesCharts.length === 0 ? (
                      <div style={{ textAlign: 'center', padding: '32px 0' }}>
                        <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>{loading ? 'Loading…' : 'No temporal columns detected'}</p>
                      </div>
                    ) : (
                      <TimeseriesSummaryCard chart={timeseriesCharts[0]} />
                    )}
                  </div>
                )}
              </div>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div className="card card-pad anim-fade-up" style={{ animationDelay: '60ms' }}>
                <p className="text-label-caps" style={{ color: 'var(--color-text-muted)', marginBottom: 14 }}>Distribution Metrics</p>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                  {[{ label: 'Mean', val: fmt(stats?.mean) }, { label: 'Median', val: fmt(stats?.median) }, { label: 'Std Dev', val: fmt(stats?.std) }, { label: 'Outliers', val: activeHistogram?.config?.is_skewed ? '⚠ Skewed' : '—' }].map((m, i) => (
                    <div key={i}>
                      <div style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 3 }}>{m.label}</div>
                      <div style={{ fontFamily: 'var(--font-display)', fontSize: 20, fontWeight: 700, color: stats ? 'var(--color-text-primary)' : 'var(--color-border)' }}>{m.val}</div>
                    </div>
                  ))}
                </div>
                <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--color-border-light)' }}>
                  {[['Min', fmt(stats?.min)], ['Max', fmt(stats?.max)], ['Skewness', fmt(stats?.skew, 3)]].map(([k, v]) => (
                    <div key={k} style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                      <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>{k}</span>
                      <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: stats ? 'var(--color-text-secondary)' : 'var(--color-text-muted)' }}>{v}</span>
                    </div>
                  ))}
                </div>
              </div>

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
                          <div key={i} style={{ background: 'var(--color-base)', borderRadius: 6, padding: '8px 10px', cursor: 'pointer' }} onClick={() => {
                            const tab = { HISTOGRAM: 'Numeric', BOXPLOT: 'Numeric', BAR: 'Categorical', PIE: 'Categorical', HEATMAP: 'Dataset', SCATTER: 'Dataset', TIMESERIES: 'Temporal', NULL_MATRIX: 'Dataset', RADAR: 'Dataset' }[c.type] ?? 'Numeric'
                            setViewMode('single')
                            if (tab) setActiveTab(tab)
                            if (c.type === 'BAR' || c.type === 'PIE') { if (c.config?.x_col) setActiveBarCol(c.config.x_col) }
                            else if (c.type === 'BOXPLOT') { if (c.config?.y_col) setActiveCol(c.config.y_col) }
                            else if (c.config?.x_col) setActiveCol(c.config.x_col)
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
                            <button key={`sk-${i}`} onClick={() => {
                              setViewMode('single')
                              if (sc.intended_chart_type === 'BAR') { setActiveTab('Categorical'); setActiveBarCol(sc.column_name) }
                              else { setActiveTab('Numeric'); setActiveCol(sc.column_name) }
                            }} style={{ borderLeft: '3px solid #E11D48', background: '#FFF1F2', borderRadius: '0 6px 6px 0', border: 'none', borderLeftWidth: 3, borderLeftStyle: 'solid', borderLeftColor: '#E11D48', padding: '8px 10px', textAlign: 'left', cursor: 'pointer', width: '100%' }}>
                              <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: '#E11D48', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700, marginBottom: 3 }}>⊘ Chart cannot be generated</div>
                              <div style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: '#1C1917', fontWeight: 500, marginBottom: 2 }}>{sc.column_name}</div>
                              <div style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: '#6B6560', lineHeight: 1.4 }}>{sc.reason}</div>
                            </button>
                          ))}
                        </>
                      )}
                    </>
                  )}
                </div>
              </div>

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