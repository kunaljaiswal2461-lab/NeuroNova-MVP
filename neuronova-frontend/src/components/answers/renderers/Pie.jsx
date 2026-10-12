import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'
import { plotNumber } from '../../charts/databaseChartSpec'
import { ExactTooltipBox } from '../tooltip'
import { SERIES_COLORS, cellString, resultView, shortLabel } from '../format'
import { ChartFrame, eligibleFor } from './shared'

export const eligible = eligibleFor

function Tip({ active, payload }) {
  const slice = active && Array.isArray(payload) ? payload[0]?.payload : null
  if (!slice) return null
  return <ExactTooltipBox title={slice.label} lines={[`Exact value: ${slice.exact}`, `Share: ${slice.share}`]} />
}

export default function PieRenderer({ result, spec }) {
  const { rows, index } = resultView(result)
  const lp = index(spec.encodings.label)
  const vp = index(spec.encodings.value)
  const total = rows.reduce((sum, row) => sum + plotNumber(row[vp]).approximation, 0)
  // Shares are for display only; the exact value is in the tooltip and the table.
  const slices = rows.map(row => {
    const approximation = plotNumber(row[vp]).approximation
    return {
      label: cellString(row[lp]),
      value: approximation,
      exact: cellString(row[vp]),
      share: total > 0 ? `${((approximation / total) * 100).toFixed(1)}%` : '—',
    }
  })
  return (
    <ChartFrame label={`Pie chart of ${spec.encodings.value} by ${spec.encodings.label}`} note="Percentages are for display only; tooltips and the table hold the exact values">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie data={slices} dataKey="value" nameKey="label" outerRadius="75%" isAnimationActive={false}>
            {slices.map((slice, position) => <Cell key={slice.label + position} fill={SERIES_COLORS[position % SERIES_COLORS.length]} />)}
          </Pie>
          <Tooltip content={<Tip />} />
          <Legend formatter={value => shortLabel(value)} />
        </PieChart>
      </ResponsiveContainer>
    </ChartFrame>
  )
}
