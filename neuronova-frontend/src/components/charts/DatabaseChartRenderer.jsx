import { Component } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

function compactLabel(value, maximum = 26) {
  const text = String(value ?? '')
  return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text
}

function plottedNumber(value) {
  const number = Number(value)
  return Number.isFinite(number)
    ? number.toLocaleString(undefined, { maximumFractionDigits: 6 })
    : '—'
}

function profileCoverageText(coverage) {
  if (!coverage?.population) return null
  const method = String(coverage.sampling_method || '').replace(/_/g, ' ')
  const sample = coverage.sample_percent == null ? null : `${coverage.sample_percent}% target`
  const limit = coverage.sample_limit == null ? null : `maximum ${coverage.sample_limit} observed rows`
  const collected = coverage.collected_at ? `collected ${coverage.collected_at}` : null
  const limitations = Array.isArray(coverage.warnings) && coverage.warnings.length
    ? `limitations: ${coverage.warnings.join(', ').replaceAll('_', ' ')}`
    : null
  return [
    'Observed sample only',
    coverage.observed_row_count == null ? null : `${coverage.observed_row_count} observed rows`,
    method || null,
    sample,
    limit,
    collected,
    limitations,
  ].filter(Boolean).join(' · ')
}

function resultCoverageText(coverage) {
  if (!coverage) return null
  if (coverage.safety_truncated) return 'Safety-truncated displayed result; the chart does not represent a complete result.'
  if (coverage.explicit_top_n) return `Requested Top-${coverage.requested_limit ?? 'N'} result; chart covers only that requested ranking.`
  if (coverage.complete_for_requested_calculation) return 'Complete result for the requested calculation.'
  return 'Chart covers only the displayed authorized result rows.'
}

function ExactValueTooltip({ active, payload, spec }) {
  const point = active && Array.isArray(payload) ? payload[0]?.payload : null
  if (!point) return null
  const isProfile = Boolean(spec?.coverage?.population)
  return (
    <div style={{ maxWidth: 300, padding: '8px 10px', border: '1px solid var(--color-border)', borderRadius: 6, background: 'white', boxShadow: 'var(--shadow-card)' }}>
      <strong style={{ display: 'block', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)', overflowWrap: 'anywhere' }}>{point.xExact}</strong>
      <span style={{ display: 'block', marginTop: 4, fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-secondary)' }}>
        {isProfile ? 'Exact observed null rate' : 'Exact returned value'}: {point.valueExact}
      </span>
      {isProfile && <span style={{ display: 'block', marginTop: 3, fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{point.nullCount} observed nulls out of {point.observedRowCount} observed rows</span>}
    </div>
  )
}

class ChartFailureBoundary extends Component {
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
    if (this.state.failed) {
      return (
        <p role="status" style={{ margin: 0, padding: '10px 12px', border: '1px solid #FDE68A', borderRadius: 6, background: '#FFFBEB', fontFamily: 'var(--font-body)', fontSize: 12, color: '#92400E' }}>
          The chart could not be rendered. The exact table result remains available and no query was rerun.
        </p>
      )
    }
    return this.props.children
  }
}

function ChartCanvas({ spec }) {
  const isLine = spec.chart_type === 'line'
  const profile = Boolean(spec.coverage?.population)
  return (
    <div role="img" aria-label={`${spec.title} ${spec.chart_type} chart`} style={{ width: '100%', height: 280, minWidth: 0 }}>
      <ResponsiveContainer width="100%" height="100%">
        {isLine ? (
          <LineChart data={spec.points} margin={{ top: 14, right: 18, left: 2, bottom: 48 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border-light)" />
            <XAxis
              dataKey="label"
              interval={0}
              angle={-28}
              textAnchor="end"
              height={62}
              tick={{ fontFamily: 'var(--font-data)', fontSize: 10, fill: 'var(--color-text-muted)' }}
              tickFormatter={compactLabel}
            />
            <YAxis
              tick={{ fontFamily: 'var(--font-data)', fontSize: 10, fill: 'var(--color-text-muted)' }}
              tickFormatter={plottedNumber}
              width={72}
            />
            <Tooltip content={<ExactValueTooltip spec={spec} />} />
            <Line
              type="monotone"
              dataKey="valueApproximation"
              name={spec.series?.[0]?.label || spec.axes?.y_label || 'Value'}
              stroke="var(--color-primary-indigo)"
              strokeWidth={2}
              dot={{ r: 3 }}
              isAnimationActive={false}
            />
          </LineChart>
        ) : (
          <BarChart data={spec.points} margin={{ top: 14, right: 18, left: 2, bottom: 48 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border-light)" />
            <XAxis
              dataKey="label"
              interval={0}
              angle={-28}
              textAnchor="end"
              height={62}
              tick={{ fontFamily: 'var(--font-data)', fontSize: 10, fill: 'var(--color-text-muted)' }}
              tickFormatter={compactLabel}
            />
            <YAxis
              tick={{ fontFamily: 'var(--font-data)', fontSize: 10, fill: 'var(--color-text-muted)' }}
              tickFormatter={plottedNumber}
              width={72}
            />
            <Tooltip content={<ExactValueTooltip spec={spec} />} />
            <ReferenceLine y={0} stroke="var(--color-text-muted)" />
            <Bar
              dataKey="valueApproximation"
              name={spec.series?.[0]?.label || spec.axes?.y_label || 'Value'}
              fill="var(--color-primary-indigo)"
              radius={[3, 3, 0, 0]}
              isAnimationActive={false}
            />
          </BarChart>
        )}
      </ResponsiveContainer>
      <p style={{ margin: '3px 0 0', fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>
        {spec.axes?.x_label} → · {spec.axes?.y_label} ↑{profile ? ' · tooltip values retain the exact observed rate' : ' · tooltip values retain the exact returned value'}
      </p>
    </div>
  )
}

export function DatabaseChartRenderer({ spec, onFailure }) {
  if (!spec?.eligible) return null
  const coverageText = spec.coverage?.population
    ? profileCoverageText(spec.coverage)
    : resultCoverageText(spec.coverage)
  return (
    <section aria-label={`${spec.title} chart`} style={{ border: '1px solid var(--color-border)', borderRadius: 8, padding: '12px', background: 'white' }}>
      <h6 style={{ margin: 0, fontFamily: 'var(--font-heading)', fontSize: 13, color: 'var(--color-text-primary)' }}>{spec.title}</h6>
      {coverageText && <p style={{ margin: '5px 0 0', fontFamily: 'var(--font-data)', fontSize: 10, lineHeight: 1.5, color: 'var(--color-text-secondary)' }}>{coverageText}</p>}
      {spec.time?.semantics && <p style={{ margin: '5px 0 0', fontFamily: 'var(--font-body)', fontSize: 11, lineHeight: 1.45, color: 'var(--color-text-muted)' }}>{spec.time.semantics}</p>}
      {spec.profile_sampling_notice && <p style={{ margin: '5px 0 0', fontFamily: 'var(--font-body)', fontSize: 11, lineHeight: 1.45, color: '#92400E' }}>{spec.profile_sampling_notice}</p>}
      <div style={{ marginTop: 10 }}>
        <ChartFailureBoundary onFailure={onFailure}>
          <ChartCanvas spec={spec} />
        </ChartFailureBoundary>
      </div>
    </section>
  )
}
