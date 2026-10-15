import { CartesianGrid, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from 'recharts'
import { plotNumber } from '../../charts/databaseChartSpec'
import { columnFormat } from '../display/chartFormat'
import { FALLBACK_TIME_ZONE } from '../display/locale'
import { ExactTooltipBox } from '../tooltip'
import { cellString, resultView, tickStyle } from '../format'
import { ChartFrame, eligibleFor } from './shared'

export const eligible = eligibleFor

function Tip({ active, payload, xName, yName, fx, fy }) {
  const point = active && Array.isArray(payload) ? payload[0]?.payload : null
  if (!point) return null
  return <ExactTooltipBox lines={[`${xName}: ${fx.line(point.exactX)}`, `${yName}: ${fy.line(point.exactY)}`]} />
}

export default function ScatterRenderer({ result, spec }) {
  const { rows, index } = resultView(result)
  const timeZone = spec?.display_time_zone || FALLBACK_TIME_ZONE
  const fx = columnFormat(result, spec.encodings.x, timeZone)
  const fy = columnFormat(result, spec.encodings.y?.[0], timeZone)
  const xp = index(spec.encodings.x)
  const yp = index(spec.encodings.y?.[0])
  const points = rows.map(row => ({
    x: plotNumber(row[xp]).approximation, y: plotNumber(row[yp]).approximation,
    exactX: cellString(row[xp]), exactY: cellString(row[yp]),
  }))
  const xName = spec.encodings.x
  const yName = spec.encodings.y?.[0]
  return (
    <ChartFrame label={`Scatter chart of ${yName} against ${xName}`} note="Tooltip values retain the exact returned values">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 14, right: 18, left: 2, bottom: 24 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border-light)" />
          <XAxis type="number" dataKey="x" name={xName} tick={tickStyle} tickFormatter={fx.tick} />
          <YAxis type="number" dataKey="y" name={yName} tick={tickStyle} width={72} tickFormatter={fy.tick} />
          <Tooltip content={<Tip xName={xName} yName={yName} fx={fx} fy={fy} />} />
          <Scatter data={points} fill="var(--color-primary-indigo)" isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
    </ChartFrame>
  )
}
