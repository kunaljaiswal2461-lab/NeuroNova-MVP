// Plain-language reason for the one automatic correction (SPEC-10 D6). The code may carry a suffix
// ("query_error:42803"); only the part before the colon names the reason.
const GROUPS = [
  [['column_not_found', 'ambiguous_column'], "a column name didn't match"],
  [['table_not_in_scope', 'ambiguous_table', 'database_qualified_table', 'no_table_reference'], "a table wasn't available"],
  [['query_error'], 'the database rejected the query'],
  [['query_too_expensive'], 'the query was too expensive'],
  [['result_row_too_large'], 'the result rows were too large'],
  [['unsupported_output_type'], "a column type can't be shown"],
  [['envelope_invalid', 'parse_error', 'unsupported_syntax', 'multiple_statements', 'unsupported_statement', 'too_complex', 'non_literal_limit'], 'the first draft was malformed'],
  [['function_not_allowed', 'schema_qualified_function', 'cast_type_not_allowed', 'table_function_not_allowed'], 'it used something not allowed'],
  [['schema_changed'], 'the database structure changed'],
]

export function repairReasonLabel(code) {
  const base = String(code ?? '').split(':')[0]
  return GROUPS.find(([codes]) => codes.includes(base))?.[1] ?? 'it was rejected'
}
