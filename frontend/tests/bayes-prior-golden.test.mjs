// The golden test of the priors' marginals (docs/specs/bayes-prior-preview.md, «The golden test against Stan», lot P3):
// the browser's ancestral draws (bayesDrawMarginals of composables/bayesPriorCore.ts, the code the worker runs, here under
// Node) against Stan's `fixed_param` draws of the same graph, quantity by quantity, by a two-sample Kolmogorov-Smirnov
// test (4,000 draws against 4,000; the asymptotic distribution of the statistic), the p-values Holm-adjusted over every
// comparison of the file and compared with the suite's alpha 0.001. The seeds are fixed, so the verdict is deterministic.
// This is a test of the software (a hyperprior drawn in the wrong order, a parametrization, a truncation), not a verdict
// of any report.
//
// The fixtures (tests/fixtures/bayes-prior/<case>.json) are written by gateway-go/scripts/bayes-prior-fixtures.sh in the
// group's image (CmdStan 2.40.0); the suite never runs CmdStan. A draw Stan wrote as nan or inf (a truncation with no mass
// at its parents' draw: the browser's rejected draw) is left out on both sides.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

register('./alias-hooks.mjs', import.meta.url)
const { bayesGraphReadText, bayesPriorPlan } = await import('~/composables/bayesGraph.ts')
const { bayesDrawMarginals } = await import('~/composables/bayesPriorCore.ts')

const HERE = dirname(fileURLToPath(import.meta.url))
const FIXTURES = join(HERE, 'fixtures', 'bayes-prior')
const GRAPHS = [
  join(HERE, '..', '..', 'gateway-go', 'internal', 'bayescodegen', 'testdata', 'golden'),
  join(HERE, '..', '..', 'gateway-go', 'internal', 'bayeschecks', 'testdata', 'golden'),
]
const ALPHA = 0.001
const SEED = 20261124

function graphText (name) {
  for (const dir of GRAPHS) {
    for (const f of [`${name}.graph.json`, `checks_${name}.graph.json`]) {
      if (existsSync(join(dir, f))) return readFileSync(join(dir, f), 'utf8')
    }
  }
  throw new Error(`no graph for the case ${name}`)
}

/** The two-sample Kolmogorov-Smirnov statistic's p-value, by its asymptotic distribution (Kolmogorov's series). */
function ks2 (a, b) {
  const x = Float64Array.from(a).sort()
  const y = Float64Array.from(b).sort()
  const n = x.length
  const m = y.length
  let i = 0
  let j = 0
  let d = 0
  while (i < n && j < m) {
    const v = Math.min(x[i], y[j])
    while (i < n && x[i] <= v) i++
    while (j < m && y[j] <= v) j++
    d = Math.max(d, Math.abs(i / n - j / m))
  }
  const en = Math.sqrt((n * m) / (n + m))
  const lambda = (en + 0.12 + 0.11 / en) * d
  let sum = 0
  for (let k = 1; k <= 100; k++) sum += 2 * (k % 2 ? 1 : -1) * Math.exp(-2 * k * k * lambda * lambda)
  return { d, p: Math.min(1, Math.max(0, sum)) }
}

/** Holm's step-down adjustment of a family of p-values. */
function holm (pvalues) {
  const order = pvalues.map((p, i) => [p, i]).sort((u, v) => u[0] - v[0])
  const adjusted = new Array(pvalues.length)
  let running = 0
  order.forEach(([p, i], rank) => {
    running = Math.max(running, Math.min(1, (pvalues.length - rank) * p))
    adjusted[i] = running
  })
  return adjusted
}

const fixtureFiles = existsSync(FIXTURES) ? readdirSync(FIXTURES).filter(f => f.endsWith('.json')).sort() : []

test('the fixtures of the Stan half are committed', { skip: fixtureFiles.length ? false : 'no fixtures yet (gateway-go/scripts/bayes-prior-fixtures.sh)' }, () => {
  assert.ok(fixtureFiles.length >= 15, `${fixtureFiles.length} fixtures`)
})

// every comparison of the file, in one family
const rows = []

for (const file of fixtureFiles) {
  const fx = JSON.parse(readFileSync(join(FIXTURES, file), 'utf8'))
  test(`${fx.case}: header, graph and the browser's draws`, () => {
    assert.equal(fx.format, 1)
    assert.equal(fx.cmdstan, '2.40.0')
    assert.equal(fx.chains * fx.draws, 4000)
    const { model } = bayesGraphReadText(graphText(fx.case))
    assert.ok(model, 'the graph reads')
    const plan = bayesPriorPlan(model)
    const mine = bayesDrawMarginals(plan, SEED, 4000)
    const nodes = Object.keys(fx.quantities)
    assert.ok(nodes.length > 0)
    for (const node of nodes) {
      // Stan's marginal of a state is its first value: the plan's `<state>.init`
      const drawn = mine[node] ?? mine[`${node}.init`]
      assert.ok(drawn, `${fx.case}: the browser draws no ${node} (Stan's fixture has it)`)
      const stan = fx.quantities[node].map(Number).filter(Number.isFinite)
      const here = Array.from(drawn).filter(Number.isFinite)
      assert.ok(stan.length >= 3000, `${fx.case}/${node}: Stan's finite draws ${stan.length}`)
      assert.ok(here.length >= 3000, `${fx.case}/${node}: the browser's finite draws ${here.length}`)
      const { d, p } = ks2(here, stan)
      rows.push({ case: fx.case, node, d, p, n: here.length, m: stan.length })
    }
  })
}

test('every comparison agrees: Holm-adjusted two-sample Kolmogorov-Smirnov p-values against alpha 0.001', { skip: fixtureFiles.length ? false : 'no fixtures yet' }, () => {
  const adj = holm(rows.map(r => r.p))
  rows.forEach((r, i) => { r.adj = adj[i] })
  if (process.env.BAYES_GOLDEN_TABLE === '1') {
    for (const r of rows) console.log(`${r.case.padEnd(26)} ${r.node.padEnd(24)} D=${r.d.toFixed(4)} p=${r.p.toExponential(2)} holm=${r.adj.toExponential(2)} n=${r.n}/${r.m}`)
  }
  const bad = rows.filter(r => r.adj < ALPHA)
  assert.deepEqual(bad.map(r => `${r.case}/${r.node} D=${r.d.toFixed(4)} p=${r.adj.toExponential(2)}`), [])
  assert.ok(rows.length >= 80, `${rows.length} comparisons`)
})
