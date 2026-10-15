// SPEC-10 W5: segments, the zero/one/many row cases, keyboard tabs, copy, the basic-summary chip.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import AnswerView from '../src/components/answers/AnswerView'

const NO = { x: null, y: [], series: null, label: null, value: null }
const dec = value => ({ kind: 'decimal', value })
const int = value => ({ kind: 'integer', value })
const text = value => ({ kind: 'text', value })

function result(columns, rows, extra = {}) {
  return {
    status: 'succeeded', execution_id: 'abc123', columns: columns.map(([alias, type]) => ({ alias, actual_postgres_type: type })),
    rows, returned_row_count: rows.length, complete: true, duration_ms: 3, warnings: [], ordering: [], ...extra,
  }
}
const answer = (presentation, extra = {}) => ({ route: 'sql', sql: 'SELECT 1', assumptions: [], presentation: { takeaways: [], caveats: [], table_columns: [], fallback: null, derived: {}, ...presentation }, ...extra })

afterEach(cleanup)

it('formats prose values from segments, keeps the exact value on hover, and falls back for a v1 row (E29, E40)', () => {
  const r = result([['revenue', 'numeric']], [[dec('1234567.50')]])
  const segs = [{ t: 'text', text: 'Revenue was ' }, { t: 'value', exact: '1234567.50', kind: 'decimal', source: 'row', column: 'revenue' }, { t: 'text', text: '.' }]
  const { unmount } = render(<AnswerView result={r} answer={answer({ display: 'table', encodings: NO, answer: 'Revenue was 1234567.50.', answer_segments: segs })} />)
  expect(screen.getByTestId('answer-text').textContent).toBe('Revenue was 12,34,567.50.')
  expect(screen.getByText('12,34,567.50', { selector: '.nn-value' }).getAttribute('title')).toBe('1234567.50')
  unmount()
  render(<AnswerView result={r} answer={answer({ display: 'table', encodings: NO, answer: 'Revenue was 1234567.50.' })} />)
  expect(screen.getByTestId('answer-text').textContent).toBe('Revenue was 1234567.50.')
})

it('shows ≈ for a long average and the full digits in the table (E30)', () => {
  const r = result([['avg', 'numeric']], [[dec('3.3333333333333333')]])
  const segs = [{ t: 'value', exact: '3.3333333333333333', kind: 'decimal', source: 'row', column: 'avg' }]
  render(<AnswerView result={r} answer={answer({ display: 'kpi', encodings: { ...NO, value: 'avg' }, answer: '3.3333333333333333', answer_segments: segs })} />)
  expect(screen.getByTestId('answer-text').textContent).toBe('≈ 3.33')
  expect(screen.getByTestId('kpi-value').textContent).toBe('≈ 3.33')
  fireEvent.click(screen.getByRole('tab', { name: 'Table' }))
  expect(within(screen.getByRole('region', { name: 'Result table' })).getByText('3.3333333333333333')).toBeTruthy()
})

it('zero rows: prose and one line, no chart, no tabs, no could-not-be-drawn note (E24, E25)', () => {
  const r = result([['region', 'text'], ['revenue', 'numeric']], [])
  const view = render(<AnswerView result={r} answer={answer({ display: 'table', encodings: NO, answer: 'Nothing.', caveats: [] })} />)
  expect(screen.getByText('No rows matched this query.')).toBeTruthy()
  expect(screen.queryByRole('tab')).toBeNull()
  expect(screen.queryByRole('note')).toBeNull()
  view.unmount()
  render(<AnswerView result={result([['a', 'text']], [], { complete: false })} answer={answer({ display: 'bar', encodings: NO, answer: 'x' })} />)
  expect(screen.getByText(/This does not mean no rows matched/)).toBeTruthy()
  expect(screen.queryByText('No rows matched this query.')).toBeNull()
})

it('one row with an ineligible chart: the table only (E26)', () => {
  render(<AnswerView result={result([['region', 'text'], ['revenue', 'numeric']], [[text('N'), dec('1')]])} answer={answer({ display: 'bar', encodings: { ...NO, x: 'region', y: ['region'] }, answer: 'x' })} />)
  expect(screen.queryByRole('note')).toBeNull()
  expect(screen.getByRole('region', { name: 'Result table' })).toBeTruthy()
})

it('500 rows: 50 first, then Show all; the header is sticky and the box bounded (E27)', () => {
  const rows = Array.from({ length: 500 }, (_, i) => [text(`r${i}`), int(String(i))])
  render(<AnswerView result={result([['name', 'text'], ['n', 'int8']], rows)} answer={answer({ display: 'table', encodings: NO, answer: 'x' })} />)
  const box = screen.getByRole('region', { name: 'Result table' })
  expect(within(box).getAllByRole('row')).toHaveLength(51)
  expect(box.style.maxHeight).toBe('420px')
  expect(within(box).getAllByRole('columnheader')[0].style.position).toBe('sticky')
  expect(screen.getByText(/Showing 50 of 500 rows/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Show all' }))
  expect(within(box).getAllByRole('row')).toHaveLength(501)
}, 30_000)

it('right-aligns numbers, groups them, and renders empty versus null differently (E37)', () => {
  const r = result([['name', 'text'], ['n', 'int8']], [[text(''), int('1234567')], [{ kind: 'null', value: null }, int('2')]])
  render(<AnswerView result={r} answer={answer({ display: 'table', encodings: NO, answer: 'x' })} />)
  const box = screen.getByRole('region', { name: 'Result table' })
  expect(within(box).getByText('12,34,567').style.textAlign).toBe('right')
  expect(within(box).getByText('(empty)').style.fontStyle).toBe('italic')
  expect(within(box).getByText('—').getAttribute('title')).toBe('No value')
})

it('tabs move with the arrow keys (W5.2)', () => {
  const r = result([['region', 'text'], ['revenue', 'numeric']], [[text('N'), dec('1')], [text('S'), dec('2')]])
  render(<AnswerView result={r} answer={answer({ display: 'bar', encodings: { ...NO, x: 'region', y: ['revenue'] }, answer: 'x' })} />)
  const chart = screen.getByRole('tab', { name: 'Chart' })
  expect(chart.getAttribute('aria-selected')).toBe('true')
  fireEvent.keyDown(chart, { key: 'ArrowRight' })
  expect(screen.getByRole('tab', { name: 'Table' }).getAttribute('aria-selected')).toBe('true')
  fireEvent.keyDown(screen.getByRole('tab', { name: 'Table' }), { key: 'Home' })
  expect(screen.getByRole('tab', { name: 'Chart' }).getAttribute('aria-selected')).toBe('true')
})

it('copies the answer exactly as shown, and shows an error when the clipboard is denied (E48, E49)', async () => {
  const r = result([['avg', 'numeric']], [[dec('3.3333333333333333')]])
  const segs = [{ t: 'text', text: 'Average ' }, { t: 'value', exact: '3.3333333333333333', kind: 'decimal', source: 'row', column: 'avg' }]
  const writeText = vi.fn().mockResolvedValue()
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  render(<AnswerView result={r} answer={answer({ display: 'table', encodings: NO, answer: 'Average 3.3333333333333333', answer_segments: segs, takeaways: ['one'], caveats: ['careful'] })} />)
  fireEvent.click(screen.getByRole('button', { name: 'Copy answer' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith('Average ≈ 3.33\n- one\nNote: careful'))
  writeText.mockRejectedValue(new Error('denied'))
  fireEvent.click(screen.getByRole('button', { name: /Copied|Copy answer/ }))
  expect((await screen.findByRole('alert')).textContent).toMatch(/Copy failed/)
})

it('marks a fallback summary and a missing presentation as a basic summary (E22, E23)', () => {
  const r = result([['n', 'int8']], [[int('42')]])
  const { unmount } = render(<AnswerView result={r} answer={answer({ display: 'table', encodings: NO, answer: 'The read-only calculation completed with 1 returned row(s).', fallback: 'llm_unavailable' })} />)
  expect(screen.getByText('Basic summary')).toBeTruthy()
  unmount()
  render(<AnswerView result={r} text="There are 42 orders." answer={{ route: 'sql', sql: 'SELECT 1', assumptions: [], presentation: null }} />)
  expect(screen.getByTestId('answer-text').textContent).toBe('There are 42 orders.')
  expect(screen.getByText('Basic summary')).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Result table' })).toBeTruthy()
})

it('de-duplicates caveats', () => {
  render(<AnswerView result={result([['n', 'int8']], [[int('1')]])} answer={answer({ display: 'table', encodings: NO, answer: 'x', caveats: ['same', 'same', 'other'] })} />)
  expect(screen.getAllByText('same')).toHaveLength(1)
})

it('shows the could-not-be-drawn note only when the renderer threw (E28)', async () => {
  vi.resetModules()
  vi.doMock('../src/components/answers/renderers/Kpi', async () => ({ eligible: () => ({ ok: true }), default: () => { throw new Error('boom') } }))
  const { default: Fresh } = await import('../src/components/answers/AnswerView')
  const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
  render(<Fresh result={result([['n', 'int8']], [[int('1')]])} answer={answer({ display: 'kpi', encodings: { ...NO, value: 'n' }, answer: 'x' })} />)
  expect((await screen.findByRole('note')).textContent).toBe('Shown as a table because the chart could not be drawn.')
  expect(screen.getByRole('region', { name: 'Result table' })).toBeTruthy()
  spy.mockRestore()
  vi.doUnmock('../src/components/answers/renderers/Kpi')
})
