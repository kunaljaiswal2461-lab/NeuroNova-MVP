import { DatabaseChartRenderer } from '../../charts/DatabaseChartRenderer'
import { buildDatabaseResultChartSpec } from '../../charts/databaseChartSpec'
import { columnFormat } from '../display/chartFormat'
import { FALLBACK_TIME_ZONE } from '../display/locale'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

// The existing Stage 6B renderer: a bar chart for a category + number result.
export default function BarRenderer({ result, spec, onFailure }) {
  const chart = buildDatabaseResultChartSpec(result)
  const timeZone = spec?.display_time_zone || FALLBACK_TIME_ZONE
  const format = chart.eligible ? { x: columnFormat(result, result.columns[0].alias, timeZone), y: columnFormat(result, result.columns[1].alias, timeZone) } : null
  return <DatabaseChartRenderer spec={chart} onFailure={onFailure} format={format} />
}
