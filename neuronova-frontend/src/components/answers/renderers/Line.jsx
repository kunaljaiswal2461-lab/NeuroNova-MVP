import { DatabaseChartRenderer } from '../../charts/DatabaseChartRenderer'
import { buildDatabaseResultChartSpec } from '../../charts/databaseChartSpec'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

export default function LineRenderer({ result, onFailure }) {
  const chart = buildDatabaseResultChartSpec(result)
  return <DatabaseChartRenderer spec={chart} onFailure={onFailure} />
}
