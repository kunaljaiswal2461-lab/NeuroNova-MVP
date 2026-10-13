// The browser-side shape of a model-written-SQL answer, from a reloaded transcript row (``nl_sql``) or, while
// streaming, from the sql_generated / repairing / presentation events (SPEC-09 W5.1).
export function answerFromRecord(record) {
  if (!record || typeof record !== 'object') return null
  return {
    presentation: record.presentation && typeof record.presentation === 'object' ? record.presentation : null,
    sql: typeof record.sql === 'string' ? record.sql : null,
    assumptions: Array.isArray(record.assumptions) ? record.assumptions : [],
    repaired: Boolean(record.repair),
    route: 'sql',
    outputIntent: record.output_intent ?? null,
    outcome: record.outcome ?? null,
  }
}
