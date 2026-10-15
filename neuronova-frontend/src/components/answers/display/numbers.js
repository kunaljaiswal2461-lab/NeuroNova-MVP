// String-based number formatting. Nothing here calls Number() or parseFloat() on a value to build display text
// (SPEC-10 I2): digits are split, grouped and rounded as text so a 30-digit decimal survives unchanged.
const NUMBER = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/

export function parseNumber(exact) {
  const match = NUMBER.exec(String(exact ?? '').trim())
  if (!match || (!match[2] && !match[3])) return null
  return { sign: match[1] === '-' ? '-' : '', int: match[2] || '0', frac: match[3] ?? '', exponent: match[4] ?? null }
}

// en-IN grouping: the last three digits, then groups of two.
export function groupInteger(digits) {
  const clean = digits.replace(/^0+(?=\d)/, '')
  if (clean.length <= 3) return clean
  const head = clean.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ',')
  return `${head},${clean.slice(-3)}`
}

const digitOf = char => char.charCodeAt(0) - 48

function increment(digits) {
  const out = digits.split('')
  for (let i = out.length - 1; i >= 0; i -= 1) {
    if (out[i] === '9') out[i] = '0'
    else { out[i] = String.fromCharCode(out[i].charCodeAt(0) + 1); return out.join('') }
  }
  return `1${out.join('')}`
}

// Round half-even to `places` fraction digits, on the digit string.
export function roundHalfEven(int, frac, places) {
  const kept = frac.slice(0, places).padEnd(places, '0')
  const rest = frac.slice(places)
  let up = false
  if (rest) {
    const tail = rest.slice(1).replace(/0+$/, '')
    if (rest[0] > '5' || (rest[0] === '5' && tail)) up = true
    else if (rest[0] === '5') up = digitOf(places > 0 ? kept.slice(-1) : int.slice(-1)) % 2 === 1
  }
  if (!up) return { int, frac: kept }
  const all = increment(int + kept)
  return { int: all.slice(0, all.length - places), frac: all.slice(all.length - places) }
}

const isZero = (int, frac) => /^0*$/.test(int) && /^0*$/.test(frac)

// mode 'asReturned' keeps ≤4 fraction digits as returned; 'stripOnly' strips trailing zeros of a long fraction;
// 'round' applies D2 (more than 4 digits after stripping -> 2 digits, approximate).
export function formatDecimalText(exact, mode) {
  const parsed = parseNumber(exact)
  if (!parsed || parsed.exponent !== null) return { text: String(exact), approximate: false }
  let { int, frac } = parsed
  let approximate = false
  if (frac.length > 4) {
    frac = frac.replace(/0+$/, '')
    if (frac.length > 4 && mode === 'round') {
      ({ int, frac } = roundHalfEven(int, frac, 2))
      approximate = true
    }
  }
  const sign = parsed.sign && !isZero(int, frac) ? '-' : ''
  const body = `${groupInteger(int)}${frac ? `.${frac}` : ''}`
  return { text: `${sign}${body}`, approximate }
}
