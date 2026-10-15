import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { buildDatabaseResultChartSpec } from '../../charts/databaseChartSpec'
import { columnFormat } from '../display/chartFormat'
import { FALLBACK_TIME_ZONE } from '../display/locale'
import { ExactTooltipBox } from '../tooltip'
import { shortLabel, tickStyle } from '../format'
import { ChartFrame, eligibleFor } from './shared'

export const eligible = eligibleFor

function Tip({ active, payload, fx, fy }) {
  const point = active && Array.isArray(payload) ? payload[0]?.payload : null
  if (!point) return null
  return <ExactTooltipBox title={fx.line(point.xExact)} lines={[`Value: ${fy.line(point.valueExact)}`]} />
}

export default function AreaRenderer({ result, spec }) {
  const chart = buildDatabaseResultChartSpec(result)
  if (!chart.eligible) return null
  const timeZone = spec?.display_time_zone || FALLBACK_TIME_ZONE
  const fx = columnFormat(result, result.columns[0].alias, timeZone)
  const fy = columnFormat(result, result.columns[1].alias, timeZone)
  return (
    <ChartFrame label={`${chart.title} area chart`} note={`${chart.axes.x_label} → · ${chart.axes.y_label} ↑ · tooltip values retain the exact returned value`}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={chart.points} margin={{ top: 14, right: 18, left: 2, bottom: 48 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border-light)" />
          <XAxis dataKey="label" interval="preserveStartEnd" angle={-28} textAnchor="end" height={62} tick={tickStyle} tickFormatter={value => shortLabel(fx.tick(value), 26)} />
          <YAxis tick={tickStyle} width={72} tickFormatter={fy.tick} />
          <Tooltip content={<Tip fx={fx} fy={fy} />} />
          <Area type="monotone" dataKey="valueApproximation" name={chart.axes.y_label} stroke="var(--color-primary-indigo)" fill="var(--color-primary-indigo)" fillOpacity={0.18} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </ChartFrame>
  )
}
