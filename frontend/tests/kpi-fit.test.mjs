// A KPI tile's figure is never cut (composables/kpiFit.ts): what its compact form says, and when the
// whole one still fits. The sizing itself is the style's, measured in a browser (KpiCard).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const { compactFigure, fitsWhole, FIGURE_FLOOR_PX } = await import('../composables/kpiFit.ts')

const brief = (locale) => {
  const f = new Intl.NumberFormat(locale, { notation: 'compact', maximumSignificantDigits: 3 })
  return (n) => f.format(n)
}

test('the compact figure keeps the format around the number: its currency, its unit', () => {
  assert.equal(compactFigure('€ 7,535,239,509', 7535239509.12, brief('en')), '€ 7.54B')
  assert.equal(compactFigure('€ 7,535,239,509.12', 7535239509.12, brief('en')), '€ 7.54B')
  assert.equal(compactFigure('10,214,479 pcs', 10214479, brief('en')), '10.2M pcs')
})

test('the compact figure is in the dashboard language, with its marks', () => {
  const it = compactFigure('€ 7.535.239.509,12', 7535239509.12, brief('it'))
  assert.equal(it, `€ ${brief('it')(7535239509.12)}`)
  assert.match(it, /^€ 7,54/)
  // French groups with a narrow no-break space: the whole number is one
  const fr = compactFigure('7 535 239 509 €', 7535239509, brief('fr'))
  assert.equal(fr, `${brief('fr')(7535239509)} €`)
})

test('the sign stays where the format wrote it', () => {
  assert.equal(compactFigure('-€ 7,535,239,509', -7535239509, brief('en')), '-€ 7.54B')
  assert.equal(compactFigure('(7,535,239,509)', -7535239509, brief('en')), '(7.54B)')
})

test('under a thousand, compact is three figures: the cents go, the title keeps them', () => {
  assert.equal(compactFigure('€ 737.70', 737.7, brief('en')), '€ 738')
})

test('a share is compacted as the percentage it shows', () => {
  assert.equal(compactFigure('123,456,789.0%', 1234567.89, brief('en')), '123M%')
})

test('nothing shorter to say: no compact form', () => {
  assert.equal(compactFigure('700', 700, brief('en')), null)
  assert.equal(compactFigure('—', null, brief('en')), null)
  assert.equal(compactFigure('', 5, brief('en')), null)
  assert.equal(compactFigure('n/a', 5, brief('en')), null)
  assert.equal(compactFigure('1,234,567', Number.NaN, brief('en')), null)
})

test('the whole figure fits while its width at its size is within the line, half a pixel of rounding aside', () => {
  // «€ 7,535,239,509» is about 7.6 times as wide as its size
  assert.equal(fitsWhole(7.6, FIGURE_FLOOR_PX, 275), true)
  assert.equal(fitsWhole(7.6, FIGURE_FLOOR_PX, 121), false)
  assert.equal(fitsWhole(7.6, 16, 121.6), true)
  // not measured yet: nothing is decided on it
  assert.equal(fitsWhole(0, 16, 10), true)
})
