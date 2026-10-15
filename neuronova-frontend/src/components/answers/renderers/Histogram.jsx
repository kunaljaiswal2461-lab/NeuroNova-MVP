import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { plotNumber } from '../../charts/databaseChartSpec'
import { columnFormat } from '../display/chartFormat'
import { FALLBACK_TIME_ZONE } from '../display/locale'
import { ExactTooltipBox } from '../tooltip'
import { cellString, resultView, shortLabel, tickStyle } from '../format'
import { ChartFrame, eligibleFor } from './shared'

export const eligible = eligibleFor

function Tip({ active, payload, fy }) {
  const bin = active && Array.isArray(payload) ? payload[0]?.payload : null
  if (!bin) return null
  return <ExactTooltipBox title={bin.label} lines={[`Count: ${fy.line(bin.exact)}`]} />
}

// The bins come from the SQL (a bin label plus a count). Nothing is binned in the browser.
export default function HistogramRenderer({ result, spec }) {
  const { rows, index } = resultView(result)
  const fy = columnFormat(result, spec.encodings.y?.[0], spec?.display_time_zone || FALLBACK_TIME_ZONE)
  const xp = index(spec.encodings.x)
  const yp = index(spec.encodings.y?.[0])
  const bins = rows.map(row => ({ label: cellString(row[xp]), value: plotNumber(row[yp]).approximation, exact: cellString(row[yp]) }))
  return (
    <ChartFrame label={`Histogram of ${spec.encodings.y?.[0]} by ${spec.encodings.x}`} note="Bins are computed by the query; tooltips hold the exact counts">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={bins} barCategoryGap={1} margin={{ top: 14, right: 18, left: 2, bottom: 48 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border-light)" />
          <XAxis dataKey="label" interval={0} angle={-28} textAnchor="end" height={62} tick={tickStyle} tickFormatter={value => shortLabel(value)} />
          <YAxis tick={tickStyle} width={72} tickFormatter={fy.tick} />
          <Tooltip content={<Tip fy={fy} />} />
          <Bar dataKey="value" fill="var(--color-primary-indigo)" isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  )
}
