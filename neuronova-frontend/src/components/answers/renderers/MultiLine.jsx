import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ExactTooltipBox } from '../tooltip'
import { SERIES_COLORS, pivotLong, resultView, shortLabel, tickStyle } from '../format'
import { ChartFrame, eligibleFor } from './shared'

export const eligible = eligibleFor

function Tip({ active, payload, label }) {
  if (!active || !Array.isArray(payload) || payload.length === 0) return null
  const exact = payload[0]?.payload?.exact ?? {}
  return <ExactTooltipBox title={label} lines={payload.map(item => `${item.name}: ${exact[item.name] ?? '—'}`)} />
}

export default function MultiLineRenderer({ result, spec }) {
  const { rows, index } = resultView(result)
  const { data, series } = pivotLong(rows, index(spec.encodings.x), index(spec.encodings.series), index(spec.encodings.y?.[0]))
  // Chronological order; the table keeps the returned order. ISO text sorts correctly for one kind of time column.
  const ordered = [...data].sort((a, b) => (a.x < b.x ? -1 : a.x > b.x ? 1 : 0))
  return (
    <ChartFrame label={`Line chart of ${spec.encodings.y?.[0]} over ${spec.encodings.x} by ${spec.encodings.series}`} note="Tooltip values retain the exact returned values">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={ordered} margin={{ top: 14, right: 18, left: 2, bottom: 48 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border-light)" />
          <XAxis dataKey="x" interval="preserveStartEnd" angle={-28} textAnchor="end" height={62} tick={tickStyle} tickFormatter={value => shortLabel(value, 26)} />
          <YAxis tick={tickStyle} width={72} />
          <Tooltip content={<Tip />} />
          <Legend formatter={value => shortLabel(value)} />
          {series.map(({ name, key }, position) => (
            <Line key={key} type="monotone" dataKey={key} name={name} stroke={SERIES_COLORS[position % SERIES_COLORS.length]} strokeWidth={2} dot={{ r: 2 }} connectNulls={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </ChartFrame>
  )
}
