// CSV export of the returned typed result (SPEC-09 W5.4): exact strings, empty for null, RFC 4180 quoting, a
// formula-injection guard, a UTF-8 BOM for Excel. No network call: the result is already in the page.
const FORMULA_START = /^[=+\-@\t\r]/

export function csvCell(cell) {
  if (cell == null || typeof cell !== 'object' || cell.kind === 'null' || cell.value == null) return ''
  if (cell.kind === 'boolean') return csvField(cell.value ? 'true' : 'false')
  return csvField(typeof cell.value === 'string' ? cell.value : '')
}

export function csvField(text) {
  let value = String(text ?? '')
  // A spreadsheet runs a cell that starts with one of these as a formula. A leading apostrophe makes it text.
  if (FORMULA_START.test(value)) value = `'${value}`
  return /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value
}

export function buildCsv(result) {
  const columns = Array.isArray(result?.columns) ? result.columns : []
  const rows = Array.isArray(result?.rows) ? result.rows : []
  const lines = [columns.map(column => csvField(column.alias ?? '')).join(',')]
  for (const row of rows) lines.push((Array.isArray(row) ? row : []).map(csvCell).join(','))
  return `\uFEFF${lines.join('\r\n')}\r\n`
}

export function csvFilename(result, now = new Date()) {
  const day = now.toISOString().slice(0, 10)
  const id = String(result?.execution_id ?? 'result').replace(/[^A-Za-z0-9]/g, '').slice(0, 6) || 'result'
  return `neuronova-${day}-${id}.csv`
}

export function downloadCsv(result) {
  const blob = new Blob([buildCsv(result)], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = csvFilename(result)
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
