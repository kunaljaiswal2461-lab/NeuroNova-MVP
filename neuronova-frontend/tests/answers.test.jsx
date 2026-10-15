// SPEC-09 W5: the answer view, its renderers, the CSV export and the SQL panel.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AnswerView from '../src/components/answers/AnswerView'
import { buildCsv, csvCell, csvField, csvFilename } from '../src/components/answers/csv'
import { pivotLong, shortLabel } from '../src/components/answers/format'
import SqlPanel from '../src/components/answers/SqlPanel'
import { RENDERERS } from '../src/components/answers/renderers'
import { answerFromRecord } from '../src/components/answers/answerState'

afterEach(() => cleanup())

const text = value => ({ kind: 'text', value })
const dec = value => ({ kind: 'decimal', value })
const int = value => ({ kind: 'integer', value })
const nul = () => ({ kind: 'null', value: null })

function result({ columns, rows, complete = true, extra = {} }) {
  return {
    execution_id: 'abcdef12-0000-0000-0000-000000000000',
    status: 'succeeded',
    columns: columns.map(([alias, type]) => ({ alias, logical_type: type, actual_postgres_type: type, expected_postgres_type: type })),
    rows,
    returned_row_count: rows.length,
    complete,
    duration_ms: 12.5,
    ...extra,
  }
}

const NO = { x: null, y: [], series: null, label: null, value: null }
const regionRevenue = result({
  columns: [['region', 'text'], ['revenue', 'numeric']],
  rows: [[text('North'), dec('100.50')], [text('South'), dec('200')], [text('East'), dec('50.25')]],
})

function answer(presentation, extra = {}) {
  return { presentation: { answer: 'North leads.', takeaways: [], caveats: [], table_columns: [], fallback: null, derived: {}, ...presentation }, sql: 'SELECT 1', assumptions: [], repaired: false, route: 'sql', ...extra }
}

describe('AnswerView', () => {
  it('shows the answer, takeaways, caveats and the footer', () => {
    render(<AnswerView result={regionRevenue} answer={answer({
      display: 'table', encodings: NO, answer: 'North leads with 100.50.', takeaways: ['Three regions'], caveats: ['This result is partial (a safety limit was reached). Totals may be incomplete.'],
    })} />)
    expect(screen.getByTestId('answer-text').textContent).toBe('North leads with 100.50.')
    expect(screen.getByRole('list', { name: 'Takeaways' }).textContent).toContain('Three regions')
    const caveat = screen.getByRole('status')
    expect(caveat.textContent).toContain('partial')
    expect(screen.getByText(/3 rows · complete/)).toBeTruthy()
    expect(screen.getByText('Generated SQL')).toBeTruthy()
  })

  it('renders a table display with the exact strings in the model-chosen order and names', () => {
    render(<AnswerView result={regionRevenue} answer={answer({
      display: 'table', encodings: NO,
      table_columns: [{ column: 'revenue', label: 'Money' }],
    })} />)
    const table = screen.getByRole('region', { name: 'Result table' })
    const headers = within(table).getAllByRole('columnheader').map(h => h.textContent)
    expect(headers).toEqual(['Moneynumber', 'Regiontext'])             // named first, every remaining column after; kind hint under each title
    expect(within(table).getByText('100.50')).toBeTruthy()
  })

  it('puts the table behind a tab for a chart display and switches without another request', async () => {
    render(<AnswerView result={regionRevenue} answer={answer({ display: 'bar', encodings: { ...NO, x: 'region', y: ['revenue'] } })} />)
    expect(screen.getByRole('tab', { name: 'Chart' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.queryByRole('region', { name: 'Result table' })).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: 'Table' }))
    expect(screen.getByRole('region', { name: 'Result table' })).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Chart' }))
    expect(screen.queryByRole('region', { name: 'Result table' })).toBeNull()
  })

  it('shows only the table, with no note, when the display is ineligible (the server already said why)', () => {
    const duplicate = result({ columns: [['region', 'text'], ['revenue', 'numeric']], rows: [[text('N'), dec('1')], [text('N'), dec('2')]] })
    render(<AnswerView result={duplicate} answer={answer({ display: 'bar', encodings: { ...NO, x: 'region', y: ['revenue'] } })} />)
    expect(screen.queryByRole('note')).toBeNull()
    expect(screen.getByRole('region', { name: 'Result table' })).toBeTruthy()
    expect(screen.queryByRole('tab', { name: 'Chart' })).toBeNull()
  })

  it('narrative and takeaways show text with the table behind a tab', () => {
    render(<AnswerView result={regionRevenue} answer={answer({ display: 'narrative', encodings: NO })} />)
    expect(screen.queryByRole('region', { name: 'Result table' })).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: 'Table' }))
    expect(screen.getByRole('region', { name: 'Result table' })).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Table' }))
    expect(screen.queryByRole('region', { name: 'Result table' })).toBeNull()
  })

  it('shows assumptions and the corrected-automatically chip', () => {
    render(<AnswerView result={regionRevenue} answer={answer({ display: 'table', encodings: NO }, { assumptions: ['Revenue means paid orders'], repaired: true })} />)
    expect(screen.getByRole('list', { name: 'Assumptions' }).textContent).toContain('Revenue means paid orders')
    expect(screen.getByText(/^Corrected automatically/)).toBeTruthy()
  })

  it('renders hostile answer text as text, never as markup', () => {
    const hostile = '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script>'
    const { container } = render(<AnswerView result={regionRevenue} answer={answer({
      display: 'table', encodings: NO, answer: hostile, takeaways: [hostile], caveats: [hostile],
    })} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByTestId('answer-text').textContent).toBe(hostile)
    expect(window.__pwned).toBeUndefined()
  })
})

describe('renderers', () => {
  it('has one renderer per display type, each with eligible() and a component', () => {
    expect(Object.keys(RENDERERS).sort()).toEqual([
      'area', 'bar', 'grouped_bar', 'heatmap', 'histogram', 'kpi', 'kpi_comparison', 'line', 'multi_line', 'narrative',
      'pie', 'pivot', 'ranked_list', 'scatter', 'table', 'takeaways',
    ])
    for (const { eligible, Component } of Object.values(RENDERERS)) {
      expect(typeof eligible).toBe('function')
      expect(typeof Component).toBe('function')
    }
  })

  it('kpi shows the exact string, even beyond Number precision', () => {
    const big = result({ columns: [['total', 'numeric']], rows: [[dec('12345678901234567890.123456789')]] })
    render(<AnswerView result={big} answer={answer({ display: 'kpi', encodings: { ...NO, value: 'total' } })} />)
    const kpi = screen.getByTestId('kpi-value')
    expect(kpi.getAttribute('title')).toBe('12345678901234567890.123456789')          // the exact string is one hover away
    expect(kpi.textContent.replace(/[^0-9]/g, '').startsWith('12345678901234567890')).toBe(true)   // no float rounding
  })

  it('kpi_comparison shows the server-computed delta and never computes one', () => {
    const two = result({ columns: [['cur', 'numeric'], ['prev', 'numeric']], rows: [[dec('120'), dec('100')]] })
    render(<AnswerView result={two} answer={answer({ display: 'kpi_comparison', encodings: { ...NO, y: ['cur', 'prev'] }, derived: { delta: '20', delta_pct: '20.00%' } })} />)
    expect(screen.getByTestId('kpi-current').textContent).toBe('120')
    expect(screen.getByTestId('kpi-previous').textContent).toBe('100')
    expect(screen.getByTestId('kpi-delta').textContent).toContain('20')
    expect(screen.getByTestId('kpi-delta-pct').textContent).toBe('20.00%')
  })

  it('kpi_comparison without a derived delta shows a dash, not a browser calculation', () => {
    const two = result({ columns: [['cur', 'numeric'], ['prev', 'numeric']], rows: [[dec('120'), dec('100')]] })
    render(<AnswerView result={two} answer={answer({ display: 'kpi_comparison', encodings: { ...NO, y: ['cur', 'prev'] }, derived: {} })} />)
    expect(screen.getByTestId('kpi-delta').textContent).toBe('—')
  })

  it('ranked_list numbers the rows in returned order and truncates long labels with the full text in a title', () => {
    const long = 'A very long region name that goes on and on'
    const ranked = result({ columns: [['region', 'text'], ['revenue', 'numeric']], rows: [[text(long), dec('9.5')], [text('B'), dec('1')]] })
    render(<AnswerView result={ranked} answer={answer({ display: 'ranked_list', encodings: { ...NO, label: 'region', value: 'revenue' } })} />)
    const items = screen.getAllByRole('listitem').filter(li => li.closest('ol'))
    expect(items.length).toBe(2)
    expect(items[0].textContent).toBe(`${shortLabel(long)}9.5`)
    expect(shortLabel(long).length).toBe(24)
    expect(within(items[0]).getByTitle(long)).toBeTruthy()
  })

  it('heatmap keeps the exact value in each cell title', () => {
    const grid = result({
      columns: [['r', 'text'], ['s', 'text'], ['v', 'numeric']],
      rows: [[text('a'), text('x'), dec('1.10')], [text('a'), text('y'), dec('9.99')], [text('b'), text('x'), dec('5')]],
    })
    render(<AnswerView result={grid} answer={answer({ display: 'heatmap', encodings: { ...NO, x: 'r', series: 's', value: 'v' } })} />)
    const region = screen.getByRole('region', { name: /Heatmap/ })
    expect(within(region).getByTitle('1.10').textContent).toBe('1.10')
    expect(within(region).getByTitle('9.99')).toBeTruthy()
    expect(within(region).getByTitle('no value')).toBeTruthy()
  })

  it('pivot builds rows by x and columns by series from long data', () => {
    const long = result({
      columns: [['r', 'text'], ['s', 'text'], ['v', 'int8']],
      rows: [[text('a'), text('x'), int('1')], [text('a'), text('y'), int('2')], [text('b'), text('x'), int('3')]],
    })
    render(<AnswerView result={long} answer={answer({ display: 'pivot', encodings: { ...NO, x: 'r', series: 's', value: 'v' } })} />)
    const table = screen.getByRole('region', { name: /Pivot table/ })
    expect(within(table).getAllByRole('columnheader').map(h => h.textContent)).toEqual(['r', 'x', 'y'])
    const cells = within(table).getAllByRole('row').slice(1).map(row => Array.from(row.children).map(c => c.textContent))
    expect(cells).toEqual([['a', '1', '2'], ['b', '3', '']])
  })

  it.each(['grouped_bar', 'multi_line', 'area', 'line', 'pie', 'scatter', 'histogram'])('%s mounts with an accessible name', display => {
    const fixtures = {
      grouped_bar: [[['r', 'text'], ['s', 'text'], ['v', 'int8']], [[text('a'), text('x'), int('1')]], { ...NO, x: 'r', series: 's', y: ['v'] }],
      multi_line: [[['d', 'date'], ['s', 'text'], ['v', 'int8']], [[{ kind: 'date', value: '2026-01-01' }, text('x'), int('1')]], { ...NO, x: 'd', series: 's', y: ['v'] }],
      area: [[['d', 'date'], ['v', 'int8']], [[{ kind: 'date', value: '2026-01-01' }, int('1')]], { ...NO, x: 'd', y: ['v'] }],
      line: [[['d', 'date'], ['v', 'int8']], [[{ kind: 'date', value: '2026-01-01' }, int('1')]], { ...NO, x: 'd', y: ['v'] }],
      pie: [[['r', 'text'], ['v', 'int8']], [[text('a'), int('1')]], { ...NO, label: 'r', value: 'v' }],
      scatter: [[['a', 'int8'], ['b', 'int8']], [[int('1'), int('2')]], { ...NO, x: 'a', y: ['b'] }],
      histogram: [[['bin', 'text'], ['n', 'int8']], [[text('0-10'), int('4')]], { ...NO, x: 'bin', y: ['n'] }],
    }
    const [columns, rows, encodings] = fixtures[display]
    render(<AnswerView result={result({ columns, rows })} answer={answer({ display, encodings })} />)
    expect(screen.getByRole('tab', { name: 'Chart' }).getAttribute('aria-selected')).toBe('true')
    expect(document.querySelector('[role="img"], section[aria-label], figure[aria-label]')).toBeTruthy()
  })

  it('zero rows show the existing no-rows note; one row of a table still renders', () => {
    const empty = result({ columns: [['n', 'int8']], rows: [] })
    render(<AnswerView result={empty} answer={answer({ display: 'table', encodings: NO })} />)
    expect(screen.getByText('No rows matched this query.')).toBeTruthy()
    cleanup()
    const one = result({ columns: [['n', 'int8']], rows: [[int('7')]] })
    render(<AnswerView result={one} answer={answer({ display: 'table', encodings: NO })} />)
    expect(screen.getByText('7')).toBeTruthy()
  })

  it('pivotLong keeps exact strings next to the plotted number', () => {
    const { data, series } = pivotLong(
      [[text('a'), text('x'), dec('1.10')], [text('a'), text('y'), dec('2')]], 0, 1, 2,
    )
    expect(series).toEqual([{ name: 'x', key: 'v0' }, { name: 'y', key: 'v1' }])
    expect(data[0].x).toBe('a')                     // a series called "x" must not overwrite the record's own x
    expect(data[0].exact).toEqual({ x: '1.10', y: '2' })
    expect(data[0].v0).toBe(1.1)
  })
})

describe('CSV export', () => {
  const sample = result({
    columns: [['name', 'text'], ['amount', 'numeric'], ['note', 'text']],
    rows: [
      [text('=SUM(A1)'), dec('12345678901234567890.10'), text('say "hi", ok')],
      [text('+1'), dec('-5'), text('@home')],
      [text('-x'), nul(), text('\tTabbed')],
      [text('plain\nline'), dec('0.00'), text('\rCR')],
    ],
  })

  it('never starts a cell with = + - @ tab or CR unprefixed (I13)', () => {
    const lines = buildCsv(sample).split('\r\n')
    expect(lines[1]).toBe(`'=SUM(A1),12345678901234567890.10,"say ""hi"", ok"`)
    expect(lines[2]).toBe(`'+1,'-5,'@home`)
    for (const trigger of ['=', '+', '-', '@', '\t', '\r']) {
      expect(csvField(`${trigger}x`).replace(/^"/, '').startsWith("'")).toBe(true)
    }
    for (const body of buildCsv(sample).split('\r\n').slice(1)) {
      for (const cell of body.split(',')) expect(/^[=+\-@\t\r]/.test(cell)).toBe(false)
    }
  })

  it('quotes per RFC 4180, writes null as empty, keeps exact decimals and starts with a BOM', () => {
    const csv = buildCsv(sample)
    expect(csv.charCodeAt(0)).toBe(0xfeff)
    expect(csv.startsWith('﻿name,amount,note\r\n')).toBe(true)
    expect(csv).toContain('12345678901234567890.10')
    expect(csvCell(nul())).toBe('')
    expect(csvCell({ kind: 'boolean', value: false })).toBe('false')
    expect(csvField('a,b')).toBe('"a,b"')
    expect(csvField('line\nbreak')).toBe('"line\nbreak"')
    expect(csv.endsWith('\r\n')).toBe(true)
  })

  it('names the file by day and the first six characters of the execution id', () => {
    expect(csvFilename(sample, new Date('2026-10-12T08:00:00Z'))).toBe('neuronova-2026-10-12-abcdef.csv')
  })

  it('downloads through a local blob and makes no network call', async () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const created = []
    URL.createObjectURL = vi.fn(blob => { created.push(blob); return 'blob:test' })
    URL.revokeObjectURL = vi.fn()
    render(<AnswerView result={sample} answer={answer({ display: 'table', encodings: NO })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
    expect(created).toHaveLength(1)
    expect(created[0].type).toContain('text/csv')
    expect(fetchSpy).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })
})

describe('SQL panel', () => {
  it('is collapsed by default and shows the SQL as text', () => {
    const { container } = render(<SqlPanel sql="SELECT 1" />)
    const details = container.querySelector('details')
    expect(details.open).toBe(false)
    expect(within(details).getByText('SQL used')).toBeTruthy()
    expect(container.querySelector('pre code').textContent).toBe('SELECT 1')
  })

  it('renders SQL containing markup as text and creates no element from it', () => {
    const sql = `SELECT '<img src=x onerror="window.__pwned2=1">' AS x`
    const { container } = render(<SqlPanel sql={sql} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('pre code').textContent).toBe(sql)
    expect(window.__pwned2).toBeUndefined()
  })

  it('copies through the clipboard and says so', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<SqlPanel sql="SELECT 2" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy())
    expect(writeText).toHaveBeenCalledWith('SELECT 2')
  })

  it('shows a visible error when the copy fails', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) }, configurable: true })
    render(<SqlPanel sql="SELECT 3" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Copy failed')
  })

  it('renders nothing without SQL', () => {
    const { container } = render(<SqlPanel sql={null} />)
    expect(container.firstChild).toBeNull()
  })
})

// Negative controls (SPEC-09 L1-16): the detectors below must be seen flagging a deliberately bad sample.
const FORBIDDEN_MARKUP = [/dangerouslySetInnerHTML/, /\.innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function\(/]
const findsForbiddenMarkup = source => FORBIDDEN_MARKUP.some(pattern => pattern.test(source))
const startsUnsafely = line => line.split(',').some(cell => /^[=+\-@\t\r]/.test(cell))

describe('negative controls', () => {
  it('I7: the markup detector flags a component that uses innerHTML', () => {
    expect(findsForbiddenMarkup('<div dangerouslySetInnerHTML={{ __html: text }} />')).toBe(true)
    expect(findsForbiddenMarkup('node.innerHTML = text')).toBe(true)
    expect(findsForbiddenMarkup('<p>{text}</p>')).toBe(false)
  })

  it('I13: an unguarded CSV writer would produce a formula cell, the real one does not', () => {
    const naive = values => values.join(',')
    const hostile = ['=HYPERLINK("http://evil.example")', '+1', '-1', '@x']
    expect(startsUnsafely(naive(hostile))).toBe(true)
    expect(startsUnsafely(hostile.map(csvField).join(','))).toBe(false)
  })
})

describe('I7: no raw HTML anywhere in the answer components', () => {
  const root = join(globalThis.process.cwd(), 'src', 'components', 'answers')      // vitest runs from the frontend root
  const files = []
  const walk = dir => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name)
      if (statSync(full).isDirectory()) walk(full)
      else files.push(full)
    }
  }
  walk(root)

  it('finds the components', () => {
    expect(files.length).toBeGreaterThan(15)
  })

  it.each(files.map(f => [f.slice(root.length + 1), f]))('%s has no dangerouslySetInnerHTML, innerHTML or eval', (_name, file) => {
    expect(findsForbiddenMarkup(readFileSync(file, 'utf8'))).toBe(false)
  })
})

describe('I7 (SPEC-10): the chat page is markup-safe too', () => {
  it('pages/DatabaseChat.jsx has no dangerouslySetInnerHTML, innerHTML or eval', () => {
    expect(findsForbiddenMarkup(readFileSync(join(globalThis.process.cwd(), 'src', 'pages', 'DatabaseChat.jsx'), 'utf8'))).toBe(false)
  })
})

describe('answerFromRecord', () => {
  it('reads a stored nl_sql row and ignores anything else', () => {
    expect(answerFromRecord(null)).toBeNull()
    expect(answerFromRecord('x')).toBeNull()
    const state = answerFromRecord({ sql: 'SELECT 1', assumptions: ['a'], repair: { reason_code: 'x' }, presentation: { display: 'table' }, output_intent: { type: 'bar' } })
    expect(state).toMatchObject({ sql: 'SELECT 1', assumptions: ['a'], repaired: true, route: 'sql' })
    expect(state.presentation.display).toBe('table')
    expect(answerFromRecord({ outcome: 'failed' }).presentation).toBeNull()
  })
})
