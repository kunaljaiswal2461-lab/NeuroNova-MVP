// SPEC-09 L1-13 (frontend half): the browser's C4 implementation against the fixture the backend also passes.
import { describe, expect, it } from 'vitest'
import fixture from '../../backend/tests/fixtures/display_eligibility.json'
import { OUTPUT_TYPES, checkDisplay, kindOf } from '../src/components/answers/eligibility'

describe('display eligibility (shared fixture)', () => {
  it('has the same cases the backend runs', () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(120)
    const shown = new Set(fixture.cases.map(c => c.display))
    for (const display of OUTPUT_TYPES) expect(shown.has(display)).toBe(true)
  })

  it.each(fixture.cases.map(c => [c.name, c]))('%s', (_name, c) => {
    const verdict = checkDisplay(c.display, c.encodings, c.columns, c.rows)
    expect({ ok: verdict.ok, reason: verdict.reason }).toEqual({ ok: c.ok, reason: c.reason })
  })

  it('maps postgres types to kinds like the backend', () => {
    expect(kindOf('int8')).toBe('integer')
    expect(kindOf('numeric')).toBe('decimal')
    expect(kindOf('float4')).toBe('number')
    expect(kindOf('bpchar')).toBe('text')
    expect(kindOf('timestamptz')).toBe('timestamptz')
    expect(kindOf('jsonb')).toBeNull()
  })
})
