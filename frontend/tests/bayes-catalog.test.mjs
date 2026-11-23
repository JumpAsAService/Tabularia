// The catalogue of the Bayesian editor without a browser (composables/bayesCatalog.ts and
// composables/bayesDistributions.ts): the copy is the gateway's snapshot, every distribution,
// parameter, function, support and hint has its words in the five languages, and a distribution
// has a browser binding exactly when the catalogue says the browser draws it.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const {
  BAYES_CATALOG, bayesDistribution, bayesDistributions, bayesFunction, bayesLimit, bayesInterval,
  bayesIntervalContains, bayesCanTruncate, bayesDrawsInBrowser, bayesMessageKeys,
} = await import('~/composables/bayesCatalog.ts')
const { BAYES_BINDINGS, bayesBinding } = await import('~/composables/bayesDistributions.ts')
const { default: en } = await import('../i18n/locales/en.gen.ts')
const { default: it } = await import('../i18n/locales/it.gen.ts')
const { default: de } = await import('../i18n/locales/de.gen.ts')
const { default: es } = await import('../i18n/locales/es.gen.ts')
const { default: fr } = await import('../i18n/locales/fr.gen.ts')

const LANGUAGES = { en, it, de, es, fr }
const lookup = (tree, key) => key.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), tree)

test('the copy is the gateway snapshot, byte for byte', (t) => {
  const copy = fileURLToPath(new URL('../assets/bayes-catalog.json', import.meta.url))
  const snapshot = fileURLToPath(new URL('../../gateway-go/internal/bayescatalog/catalog.json', import.meta.url))
  if (!existsSync(snapshot)) return t.skip('the gateway snapshot is not in this checkout')
  assert.ok(readFileSync(copy).equals(readFileSync(snapshot)), 'frontend/assets/bayes-catalog.json differs from the snapshot: run .github/scripts/bayes_catalog.py')
})

test('the catalogue loads and its lookups answer', () => {
  assert.equal(BAYES_CATALOG.format_version, 1)
  const ids = BAYES_CATALOG.distributions.map(d => d.id)
  assert.equal(new Set(ids).size, ids.length, 'an id twice')
  assert.equal(bayesDistribution('normal').stan.density, 'normal_lpdf')
  assert.equal(bayesDistribution('nope'), undefined)
  assert.ok(bayesDistributions('prior').every(d => d.uses.includes('prior')))
  assert.ok(bayesDistributions('likelihood').some(d => d.id === 'poisson_log'))
  assert.equal(bayesDistributions().length, ids.length)
  assert.equal(bayesFunction('log1p_exp').stan, 'log1p_exp')
  assert.equal(bayesLimit('max_name_bytes'), 40)
  assert.throws(() => bayesLimit('max_nothing'), /max_nothing/)
  for (const [key, value] of Object.entries(BAYES_CATALOG.limits)) assert.ok(value > 0, `${key} must be positive`)
})

test('intervals know their open and closed ends', () => {
  const positive = bayesInterval('positive')
  assert.equal(bayesIntervalContains(positive, 0), false)
  assert.equal(bayesIntervalContains(positive, 1e-300), true)
  assert.equal(bayesIntervalContains(bayesInterval('non_negative'), 0), true)
  assert.equal(bayesIntervalContains(bayesInterval('unit'), 1), false)
  assert.equal(bayesIntervalContains(bayesInterval('real'), -1e300), true)
  assert.equal(bayesIntervalContains(bayesInterval('real'), Number.NaN), false)
  for (const f of BAYES_CATALOG.language.functions.filter(f => !f.apart)) {
    assert.ok(bayesInterval(f.domain), `${f.name}: unknown domain ${f.domain}`)
    assert.ok(bayesInterval(f.range), `${f.name}: unknown range ${f.range}`)
  }
})

test('only a distribution with both CDFs can be truncated', () => {
  assert.equal(bayesCanTruncate(bayesDistribution('normal')), true)
  assert.equal(bayesCanTruncate(bayesDistribution('neg_binomial_2_log')), false)
})

test('every distribution, parameter, function, support and hint has its words in the five languages', () => {
  const missing = []
  for (const key of bayesMessageKeys()) {
    for (const [code, tree] of Object.entries(LANGUAGES)) {
      const text = lookup(tree, key)
      if (typeof text !== 'string' || text.trim() === '') missing.push(`${code}: ${key}`)
    }
  }
  assert.deepEqual(missing, [])
  // the keys the catalogue names for its hints are the ones the language uses
  for (const hint of BAYES_CATALOG.language.hints) assert.match(hint.message, /^bayes\.expr\.hint\./)
})

test('a distribution has a binding exactly when the browser draws it, and none other has one', () => {
  for (const d of BAYES_CATALOG.distributions) {
    assert.equal(Boolean(bayesBinding(d.id)), bayesDrawsInBrowser(d), `${d.id}: browser is ${d.browser}`)
  }
  const known = new Set(BAYES_CATALOG.distributions.map(d => d.id))
  for (const id of Object.keys(BAYES_BINDINGS)) assert.ok(known.has(id), `a binding for ${id}, which the catalogue lacks`)
  assert.equal(bayesBinding('constructor'), undefined)
})

test('every prior is drawn by the browser', () => {
  for (const d of bayesDistributions('prior')) assert.notEqual(d.browser, 'none', d.id)
})
