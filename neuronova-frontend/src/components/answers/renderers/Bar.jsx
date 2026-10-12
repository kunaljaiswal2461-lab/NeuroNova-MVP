import { DatabaseChartRenderer } from '../../charts/DatabaseChartRenderer'
import { buildDatabaseResultChartSpec } from '../../charts/databaseChartSpec'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

// The existing Stage 6B renderer: a bar chart for a category + number result.
export default function BarRenderer({ result, onFailure }) {
  const chart = buildDatabaseResultChartSpec(result)
  return <DatabaseChartRenderer spec={chart} onFailure={onFailure} />
}
