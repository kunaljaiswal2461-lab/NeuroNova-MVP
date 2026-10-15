import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { columnFormat } from '../display/chartFormat'
import { FALLBACK_TIME_ZONE } from '../display/locale'
import { ExactTooltipBox } from '../tooltip'
import { SERIES_COLORS, pivotLong, resultView, shortLabel, tickStyle } from '../format'
import { ChartFrame, eligibleFor } from './shared'

export const eligible = eligibleFor

function Tip({ active, payload, label, fx, fy }) {
  if (!active || !Array.isArray(payload) || payload.length === 0) return null
  const exact = payload[0]?.payload?.exact ?? {}
  return <ExactTooltipBox title={fx.line(label)} lines={payload.map(item => `${item.name}: ${exact[item.name] == null ? '—' : fy.line(exact[item.name])}`)} />
}

export default function GroupedBarRenderer({ result, spec }) {
  const [stacked, setStacked] = useState(false)
  const { rows, index } = resultView(result)
  const timeZone = spec?.display_time_zone || FALLBACK_TIME_ZONE
  const fx = columnFormat(result, spec.encodings.x, timeZone)
  const fy = columnFormat(result, spec.encodings.y?.[0], timeZone)
  const { data, series } = pivotLong(rows, index(spec.encodings.x), index(spec.encodings.series), index(spec.encodings.y?.[0]))
  return (
    <div>
      <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 6 }}>
        <input type="checkbox" checked={stacked} onChange={event => setStacked(event.target.checked)} /> Stacked
      </label>
      <ChartFrame label={`Grouped bar chart of ${spec.encodings.y?.[0]} by ${spec.encodings.x} and ${spec.encodings.series}`} note="Tooltip values retain the exact returned values">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 14, right: 18, left: 2, bottom: 48 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border-light)" />
            <XAxis dataKey="x" interval={0} angle={-28} textAnchor="end" height={62} tick={tickStyle} tickFormatter={value => shortLabel(fx.tick(value))} />
            <YAxis tick={tickStyle} width={72} tickFormatter={fy.tick} />
            <Tooltip content={<Tip fx={fx} fy={fy} />} />
            <Legend formatter={value => shortLabel(value)} />
            {series.map(({ name, key }, position) => (
              <Bar key={key} dataKey={key} name={name} stackId={stacked ? 'stack' : undefined} fill={SERIES_COLORS[position % SERIES_COLORS.length]} isAnimationActive={false} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </div>
  )
}
