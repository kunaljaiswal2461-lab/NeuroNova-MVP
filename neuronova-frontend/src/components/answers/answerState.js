// The browser-side shape of a model-written-SQL answer, from a reloaded transcript row (``nl_sql``) or, while
// streaming, from the sql_generated / repairing / presenting / presentation events (SPEC-09 W5.1, SPEC-10 W5.5).
export function answerFromRecord(record) {
  if (!record || typeof record !== 'object') return null
  const repair = record.repair && typeof record.repair === 'object' ? record.repair : null
  return {
    presentation: record.presentation && typeof record.presentation === 'object' ? record.presentation : null,
    sql: typeof record.sql === 'string' ? record.sql : null,
    firstSql: typeof repair?.original_sql === 'string' ? repair.original_sql : null,
    assumptions: Array.isArray(record.assumptions) ? record.assumptions : [],
    repaired: Boolean(repair),
    repair: repair ? { reasonCode: repair.reason_code ?? null } : null,
    route: 'sql',
    outputIntent: record.output_intent ?? null,
    outcome: record.outcome ?? null,
    failureCode: record.failure_code ?? null,
  }
}
