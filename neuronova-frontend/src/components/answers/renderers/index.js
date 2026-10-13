import Area, { eligible as areaEligible } from './Area'
import Bar, { eligible as barEligible } from './Bar'
import GroupedBar, { eligible as groupedBarEligible } from './GroupedBar'
import Heatmap, { eligible as heatmapEligible } from './Heatmap'
import Histogram, { eligible as histogramEligible } from './Histogram'
import Kpi, { eligible as kpiEligible } from './Kpi'
import KpiComparison, { eligible as kpiComparisonEligible } from './KpiComparison'
import Line, { eligible as lineEligible } from './Line'
import MultiLine, { eligible as multiLineEligible } from './MultiLine'
import Narrative, { eligible as narrativeEligible } from './Narrative'
import Pie, { eligible as pieEligible } from './Pie'
import Pivot, { eligible as pivotEligible } from './Pivot'
import RankedList, { eligible as rankedListEligible } from './RankedList'
import Scatter, { eligible as scatterEligible } from './Scatter'
import Table, { eligible as tableEligible } from './Table'
import Takeaways, { eligible as takeawaysEligible } from './Takeaways'

// display type -> { eligible(result, spec), Component }. The backend decides; eligible() is the second check.
export const RENDERERS = {
  kpi: { eligible: kpiEligible, Component: Kpi },
  kpi_comparison: { eligible: kpiComparisonEligible, Component: KpiComparison },
  table: { eligible: tableEligible, Component: Table },
  ranked_list: { eligible: rankedListEligible, Component: RankedList },
  bar: { eligible: barEligible, Component: Bar },
  grouped_bar: { eligible: groupedBarEligible, Component: GroupedBar },
  line: { eligible: lineEligible, Component: Line },
  multi_line: { eligible: multiLineEligible, Component: MultiLine },
  area: { eligible: areaEligible, Component: Area },
  pie: { eligible: pieEligible, Component: Pie },
  scatter: { eligible: scatterEligible, Component: Scatter },
  histogram: { eligible: histogramEligible, Component: Histogram },
  heatmap: { eligible: heatmapEligible, Component: Heatmap },
  pivot: { eligible: pivotEligible, Component: Pivot },
  narrative: { eligible: narrativeEligible, Component: Narrative },
  takeaways: { eligible: takeawaysEligible, Component: Takeaways },
}
