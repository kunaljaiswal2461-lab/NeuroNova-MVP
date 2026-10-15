import { describe, expect, it } from 'vitest'
import { formatValue } from '../src/components/answers/display/formatValue'
import { columnProfile, columnHint, columnTitle } from '../src/components/answers/display/columnProfile'
import { axisTick } from '../src/components/answers/display/axisTick'
import { groupInteger, roundHalfEven } from '../src/components/answers/display/numbers'

const prose = { surface: 'prose' }
const table = { surface: 'table' }
const f = (exact, kind, ctx = prose) => formatValue(exact, kind, ctx)

describe('numbers (D4)', () => {
  it('groups integers the Indian way, including negatives and small numbers', () => {
    expect(f('1234567', 'integer').text).toBe('12,34,567')
    expect(f('-1234567', 'integer').text).toBe('-12,34,567')
    expect(f('999', 'integer').text).toBe('999')
    expect(f('1000', 'integer').text).toBe('1,000')
    expect(f('100000', 'integer').text).toBe('1,00,000')
  })
  it('keeps a short fraction as returned (E29)', () => {
    expect(f('1234567.50', 'decimal').text).toBe('12,34,567.50')
    expect(f('1234567.50', 'decimal', table).text).toBe('12,34,567.50')
    expect(f('1234567.50', 'decimal').title).toBe('1234567.50')
  })
  it('rounds a long fraction in prose with ≈, never in a table (E30)', () => {
    const p = f('3.3333333333333333', 'decimal')
    expect(p).toMatchObject({ text: '≈ 3.33', approximate: true, title: '3.3333333333333333' })
    expect(f('3.3333333333333333', 'decimal', { surface: 'kpi' }).text).toBe('≈ 3.33')
    expect(f('3.3333333333333333', 'decimal', table)).toMatchObject({ text: '3.3333333333333333', approximate: false })
  })
  it('strips trailing zeros before deciding to round', () => {
    expect(f('3.500000', 'decimal')).toMatchObject({ text: '3.5', approximate: false })
    expect(f('3.500000', 'decimal', table).text).toBe('3.5')
    expect(f('12.000000', 'number').text).toBe('12')
  })
  it('rounds half to even on the string, carrying into the integer part', () => {
    expect(roundHalfEven('2', '125000', 2)).toEqual({ int: '2', frac: '12' })
    expect(roundHalfEven('2', '135000', 2)).toEqual({ int: '2', frac: '14' })
    expect(roundHalfEven('9', '999999', 2)).toEqual({ int: '10', frac: '00' })
    expect(f('-0.0000001', 'decimal').text).toBe('≈ 0.00')   // no negative zero
  })
  it('never goes through a float: 30-digit values keep every digit (E31, I2)', () => {
    const exact = '123456789012345678901234567890'
    expect(f(exact, 'integer').text.replace(/,/g, '')).toBe(exact)
    const dec = '123456789012345678901234567.89'
    expect(f(dec, 'decimal').text.replace(/,/g, '')).toBe(dec)
    for (let i = 0; i < 1000; i += 1) {
      let digits = String(1 + (i % 9))
      for (let j = 0; j < 25 + (i % 20); j += 1) digits += String((i * 7 + j * 13) % 10)
      expect(groupInteger(digits).replace(/,/g, '')).toBe(digits)
    }
  })
  it('shows scientific and non-finite values as returned or by symbol (E32)', () => {
    expect(f('1.5e+20', 'number').text).toBe('1.5e+20')
    expect(f('2E-7', 'number').text).toBe('2E-7')
    expect(f('NaN', 'number').text).toBe('NaN')
    expect(f('Infinity', 'number').text).toBe('∞')
    expect(f('-Infinity', 'number').text).toBe('−∞')
  })
  it('formats percent, booleans, null, empty, uuid and long text', () => {
    expect(f('12.34%', 'percent').text).toBe('12.34%')
    expect(f('1234.5%', 'percent').text).toBe('1,234.5%')
    expect(f('n/a', 'text').text).toBe('n/a')
    expect(f('true', 'boolean').text).toBe('Yes')
    expect(f('false', 'boolean', table).text).toBe('false')
    expect(f(null, 'text')).toMatchObject({ text: '—', title: 'No value' })
    expect(f('—', null)).toMatchObject({ text: '—', title: 'No value' })
    expect(f('', 'text')).toMatchObject({ text: '(empty)', title: 'Empty text', empty: true })      // E37
    expect(f('123e4567-e89b-12d3-a456-426614174000', 'uuid')).toMatchObject({ text: '123e4567…', title: '123e4567-e89b-12d3-a456-426614174000' })
    const long = 'x'.repeat(5000)
    const cell = f(long, 'text', table)
    expect(cell.text).toHaveLength(200)
    expect(cell.text.endsWith('…')).toBe(true)
    expect(cell.title).toBe(long)                                                                     // E38
    expect(f(long, 'text').text).toBe(long)
  })
})

function result(type, values) {
  return {
    status: 'succeeded',
    columns: [{ alias: 'when', actual_postgres_type: type }],
    rows: values.map(value => [{ kind: type, value }]),
  }
}

describe('temporal values', () => {
  it('uses month granularity for monthly buckets across table, prose and axis (E33)', () => {
    const r = result('timestamp', ['2026-09-01T00:00:00', '2026-10-01T00:00:00', '2026-11-01T00:00:00'])
    const profile = columnProfile(r, 0)
    expect(profile.granularity).toBe('month')
    expect(f('2026-10-01T00:00:00', 'timestamp', { surface: 'table', profile }).text).toBe('Oct 2026')
    expect(f('2026-10-01T00:00:00', 'timestamp', { profile }).text).toBe('Oct 2026')
    expect(axisTick('2026-10-01T00:00:00', 'timestamp', profile)).toBe('Oct 2026')
  })
  it('uses year and day granularity', () => {
    expect(columnProfile(result('date', ['2025-01-01', '2026-01-01']), 0).granularity).toBe('year')
    const days = columnProfile(result('date', ['2026-10-01', '2026-10-02']), 0)
    expect(days.granularity).toBe('day')
    expect(f('2026-10-02', 'date', { profile: days }).text).toBe('2 Oct 2026')
  })
  it('shows a single midnight value at full precision (E34)', () => {
    const r = result('timestamp', ['2026-10-01T00:00:00'])
    const profile = columnProfile(r, 0)
    expect(profile.granularity).toBe('full')
    expect(f('2026-10-01T00:00:00', 'timestamp', { profile }).text).toBe('1 Oct 2026, 00:00')
  })
  it('shows minutes, and seconds only when some value has them', () => {
    expect(columnProfile(result('timestamp', ['2026-10-01T14:05:00', '2026-10-01T15:00:00']), 0).granularity).toBe('datetime')
    const withSeconds = columnProfile(result('timestamp', ['2026-10-01T14:05:09', '2026-10-01T15:00:00']), 0)
    expect(withSeconds.granularity).toBe('datetime_seconds')
    expect(f('2026-10-01T14:05:09.123456', 'timestamp', { profile: withSeconds }).text).toBe('1 Oct 2026, 14:05:09')
  })
  it('converts timestamptz to the display zone and names it (E35)', () => {
    const out = f('2026-10-13T20:00:00Z', 'timestamptz', { surface: 'prose', timeZone: 'Asia/Kolkata' })
    expect(out.text).toBe('14 Oct 2026, 01:30 IST')
    expect(out.title).toBe('2026-10-13 20:00:00 UTC')
    expect(f('2026-10-13T20:00:00Z', 'timestamptz', { surface: 'table', timeZone: 'Asia/Kolkata' }).text).toBe('14 Oct 2026, 01:30')
  })
  it('infers granularity after the zone conversion', () => {
    // 18:30 UTC is midnight in IST: every value is the 1st at 00:00 there -> months.
    const r = result('timestamptz', ['2026-09-30T18:30:00Z', '2026-10-31T18:30:00Z'])
    const profile = columnProfile(r, 0, 'Asia/Kolkata')
    expect(profile.granularity).toBe('month')
    expect(f('2026-09-30T18:30:00Z', 'timestamptz', { profile, timeZone: 'Asia/Kolkata' }).text).toBe('Oct 2026')
    expect(columnHint(profile)).toBe('date (IST)')
  })
  it('does not shift a date or naive timestamp by any zone (E36)', () => {
    // Wall times are read and shown in UTC, never through the browser's zone: day boundaries stay put.
    expect(f('2026-10-01', 'date').text).toBe('1 Oct 2026')
    expect(f('2026-01-01', 'date').text).toBe('1 Jan 2026')
    expect(f('2026-12-31T23:59:00', 'timestamp').text).toBe('31 Dec 2026, 23:59')
  })
})

describe('column helpers', () => {
  it('humanises an alias for display only and prefers the pass-2 label', () => {
    expect(columnTitle('total_revenue', [])).toBe('Total revenue')
    expect(columnTitle('total_revenue', [{ column: 'total_revenue', label: 'Revenue (₹)' }])).toBe('Revenue (₹)')
    expect(columnTitle('x__y', undefined)).toBe('X y')
  })
  it('hints at the kind', () => {
    expect(columnHint(columnProfile(result('timestamptz', ['2026-10-13T20:00:00Z', '2026-10-13T21:00:00Z']), 0))).toBe('date & time (IST)')
    expect(columnHint({ kind: 'int8' })).toBe('')
    expect(columnHint({ kind: 'integer' })).toBe('number')
  })
  it('compacts numeric ticks and truncates category ticks', () => {
    expect(axisTick(12345678, 'integer')).toBe('1.2Cr')
    expect(axisTick('a'.repeat(40), 'text')).toHaveLength(24)
  })
})
