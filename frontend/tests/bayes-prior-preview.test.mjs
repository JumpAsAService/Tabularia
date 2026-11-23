// The prior preview's core (composables/bayesPriorCore.ts) under Node: closed forms against the
// bindings (to the bit), ancestral draws of mixtures with a known marginal against that marginal's
// CDF (one-sample Kolmogorov-Smirnov, Holm-adjusted over every comparison of the file, against the
// suite's alpha 0.001; the seeds are fixed, so the verdict is deterministic), the representative
// unit through an index, draws Stan would reject (left out and counted), the cut of a heavy tail,
// determinism, the pool's bounds, the answers' shapes, and the response times (measured, recorded
// by a diagnostic, never asserted).
// Small data always: 4,000 draws and graphs of a few nodes.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const { bayesBinding } = await import('~/composables/bayesDistributions.ts')
const {
  bayesPriorPreview, bayesDrawMarginals, bayesPriorPool,
  BAYES_PRIOR_DRAWS, BAYES_PRIOR_BINS, BAYES_PRIOR_CURVE_POINTS,
} = await import('~/composables/bayesPriorCore.ts')

const ALPHA = 0.001

// ---------------------------------------------------------------- building plans

const num = n => ({ kind: 'num', num: n })
const name = id => ({ kind: 'name', id })
const call = (f, x) => ({ kind: 'call', name: f, args: [x] })
const bin = (op, a, b) => ({ kind: 'binary', op, args: [a, b] })
const index = (id, by) => ({ kind: 'index', id, by, lifted: true })
const c = x => ({ constant: x })
const ast = a => ({ ast: a })

function plan (...nodes) {
  const out = { order: [], nodes: {} }
  for (const n of nodes) {
    out.order.push(n.id)
    out.nodes[n.id] = { plate: '', errors: 0, kind: 'parameter', ...n }
  }
  return out
}
const param = (id, dist, args, extra = {}) => ({ id, dist, args, ...extra })
const halfBounds = { range: { lower: 0 }, truncated: true }

// ---------------------------------------------------------------- statistics helpers

function ksPValue (sorted, cdf) {
  const n = sorted.length
  let d = 0
  for (let i = 0; i < n; i++) {
    const f = cdf(sorted[i])
    d = Math.max(d, f - i / n, (i + 1) / n - f)
  }
  const lambda = (Math.sqrt(n) + 0.12 + 0.11 / Math.sqrt(n)) * d
  let sum = 0
  for (let k = 1; k <= 100; k++) sum += 2 * (k % 2 ? 1 : -1) * Math.exp(-2 * k * k * lambda * lambda)
  return Math.min(1, Math.max(0, sum))
}

/** Holm's step-down adjustment of a family of p-values. */
function holm (pvalues) {
  const order = pvalues.map((p, i) => [p, i]).sort((a, b) => a[0] - b[0])
  const adjusted = new Array(pvalues.length)
  let running = 0
  order.forEach(([p, i], rank) => {
    running = Math.max(running, Math.min(1, (pvalues.length - rank) * p))
    adjusted[i] = running
  })
  return adjusted
}

const normalBinding = bayesBinding('normal')
const normalCdf = (x, mu, sigma) => normalBinding.cdf(x, [mu, sigma])
const sortedDraws = (draws) => Float64Array.from(draws).filter(x => !Number.isNaN(x)).sort()

// ---------------------------------------------------------------- the plans with a known marginal

const SEED = 20261123

// mu ~ normal(0, 10); x ~ normal(mu, 3): the marginal of x is normal(0, sqrt(109))
const planNormalNormal = plan(
  param('mu', 'normal', { mu: c(0), sigma: c(10) }),
  param('x', 'normal', { mu: { ref: 'mu' }, sigma: c(3) }),
)

// v ~ inv_gamma(nu/2, nu/2); s = sqrt(v); x ~ normal(0, s): the marginal of x is Student's t with nu degrees of freedom
const NU = 5
const planStudent = plan(
  param('v', 'inv_gamma', { alpha: c(NU / 2), beta: c(NU / 2) }),
  { id: 's', kind: 'transformed', value: ast(call('sqrt', name('v'))) },
  param('x', 'normal', { mu: c(0), sigma: { ref: 's' } }),
)

// normal(0, 1) cut at 0 by bounds: the folded normal
const planHalfNormal = plan(param('h', 'normal', { mu: c(0), sigma: c(1) }, halfBounds))

// a log-odds prior seen as a probability: p = inv_logit(a), a ~ normal(0, 1.5)
const planLogistic = plan(
  param('a', 'normal', { mu: c(0), sigma: c(1.5) }),
  { id: 'p', kind: 'transformed', value: ast(call('inv_logit', name('a'))) },
)

// schools in districts: mu_d ~ normal(0, 5); school ~ normal(mu_d[district], 2): the marginal of one school is normal(0, sqrt(29))
const planSchools = plan(
  param('mu_d', 'normal', { mu: c(0), sigma: c(5) }, { plate: 'district' }),
  param('school', 'normal', { mu: ast(index('mu_d', 'district')), sigma: c(2) }, { plate: 'school' }),
)

// the truncation's bound is the same for every draw but its arguments are not: m ~ normal(0, 1); y ~ normal(m, 1) cut at 0
const planCutPerDraw = plan(
  param('m', 'normal', { mu: c(0), sigma: c(1) }),
  param('y', 'normal', { mu: { ref: 'm' }, sigma: c(1) }, halfBounds),
)
// the marginal CDF of y by quadrature over m (the trapezoid on [-9, 9]): the composition of the truncated normal by hand
function cutPerDrawCdf (y) {
  const steps = 400
  let total = 0
  for (let k = 0; k <= steps; k++) {
    const m = -9 + 18 * k / steps
    const weight = (k === 0 || k === steps) ? 0.5 : 1
    const density = Math.exp(-m * m / 2) / Math.sqrt(2 * Math.PI)
    // P(y' <= y | y' > 0) = 1 - Phi(m - y) / Phi(m), by the survival functions so the far tail does not divide 0 by 0
    const conditional = 1 - normalCdf(m - y, 0, 1) / normalCdf(m, 0, 1)
    total += weight * density * conditional
  }
  return total * 18 / steps
}

const comparisons = [
  { label: 'normal(mu, 3), mu ~ normal(0, 10) is normal(0, sqrt(109))', plan: planNormalNormal, target: 'x', cdf: x => normalCdf(x, 0, Math.sqrt(109)) },
  { label: 'normal(0, sqrt(v)), v ~ inv_gamma(nu/2, nu/2) is Student t(nu)', plan: planStudent, target: 'x', cdf: x => bayesBinding('student_t').cdf(x, [NU, 0, 1]) },
  { label: 'a half-normal by bounds is the folded normal (a constant-argument draw)', plan: planHalfNormal, target: 'h', cdf: x => 2 * normalCdf(x, 0, 1) - 1 },
  { label: 'inv_logit(a), a ~ normal(0, 1.5) is Phi(logit(p) / 1.5)', plan: planLogistic, target: 'p', cdf: p => normalCdf(Math.log(p / (1 - p)) / 1.5, 0, 1) },
  { label: 'one school through its district by an index is normal(0, sqrt(29))', plan: planSchools, target: 'school', cdf: x => normalCdf(x, 0, Math.sqrt(29)) },
  { label: 'a truncation by inversion with each draw\'s own arguments is the quadrature of the composition', plan: planCutPerDraw, target: 'y', cdf: cutPerDrawCdf },
]

test('ancestral draws of the mixtures with a known marginal: one-sample KS, Holm over all, alpha 0.001', () => {
  const pvalues = comparisons.map((k) => {
    const draws = bayesDrawMarginals(k.plan, SEED)[k.target]
    assert.equal(draws.length, BAYES_PRIOR_DRAWS)
    assert.ok(draws.every(x => !Number.isNaN(x)), `${k.label}: draws were left out`)
    return ksPValue(sortedDraws(draws), k.cdf)
  })
  const adjusted = holm(pvalues)
  comparisons.forEach((k, i) => assert.ok(adjusted[i] > ALPHA, `${k.label}: Holm-adjusted p = ${adjusted[i]} (raw ${pvalues[i]})`))
})

test('the same marginals through the preview itself: the histogram is the draws counted and the quantiles are the draws\'', () => {
  for (const k of comparisons) {
    const answer = bayesPriorPreview(k.plan, k.target, SEED)
    if (k.plan.nodes[k.target].args && Object.values(k.plan.nodes[k.target].args).every(s => 'constant' in s)) continue
    assert.equal(answer.kind, 'histogram', k.label)
    assert.equal(answer.y.length, BAYES_PRIOR_BINS)
    assert.equal(answer.x.length, BAYES_PRIOR_BINS + 1)
    const inside = answer.y.reduce((a, b) => a + b, 0)
    assert.equal(inside + answer.cut.below + answer.cut.above, answer.used, k.label)
    assert.ok(answer.quantiles.q5 < answer.quantiles.q50 && answer.quantiles.q50 < answer.quantiles.q95, k.label)
    const draws = sortedDraws(bayesDrawMarginals(k.plan, SEED)[k.target])
    // type 7 by hand at the median of 4,000 values: the mean of the two middle ones
    assert.equal(answer.quantiles.q50, (draws[1999] + draws[2000]) / 2, k.label)
    assert.equal(answer.mixture, true)
  }
})

// ---------------------------------------------------------------- closed forms

test('a closed-form curve and its quantiles are the bindings\' own, to the bit', () => {
  const p = plan(param('theta', 'student_t', { nu: c(4), mu: c(1), sigma: ast(bin('*', num(2), num(0.5))) }))
  const answer = bayesPriorPreview(p, 'theta', 1)
  const binding = bayesBinding('student_t')
  const args = [4, 1, 1]
  assert.equal(answer.kind, 'curve')
  assert.equal(answer.mixture, false)
  assert.equal(answer.x.length, BAYES_PRIOR_CURVE_POINTS)
  for (let k = 0; k < answer.x.length; k++) assert.equal(answer.y[k], binding.pdf(answer.x[k], args))
  assert.deepEqual(answer.quantiles, { q5: binding.quantile(0.05, args), q50: binding.quantile(0.5, args), q95: binding.quantile(0.95, args) })
  assert.equal(answer.x[0], binding.quantile(0.005, args))
  assert.equal(answer.x.at(-1), binding.quantile(0.995, args))
  assert.ok(Math.abs(answer.cutMass.below - 0.005) < 1e-12 && Math.abs(answer.cutMass.above - 0.005) < 1e-12)
  assert.equal(answer.leftOut, undefined)
})

test('closed form does not depend on the seed', () => {
  const p = plan(param('theta', 'normal', { mu: c(0), sigma: c(1) }))
  assert.deepEqual(bayesPriorPreview(p, 'theta', 1).y, bayesPriorPreview(p, 'theta', 99).y)
})

test('a truncated closed form: the density over the mass, the quantiles through the CDF\'s range', () => {
  const p = plan(param('h', 'normal', { mu: c(0), sigma: c(2) }, halfBounds))
  const answer = bayesPriorPreview(p, 'h', 1)
  const b = bayesBinding('normal')
  const args = [0, 2]
  const mass = 1 - b.cdf(0, args)
  assert.equal(answer.truncated, true)
  assert.equal(answer.quantiles.q50, b.quantile(b.cdf(0, args) + 0.5 * mass, args))
  assert.equal(answer.quantiles.q95, b.quantile(b.cdf(0, args) + 0.95 * mass, args))
  for (let k = 0; k < answer.x.length; k++) assert.equal(answer.y[k], b.pdf(answer.x[k], args) / mass)
  // the wall at 0 is near the 0.5 % quantile, so the axis starts on it
  assert.equal(answer.x[0], 0)
  assert.ok(answer.x.every(x => x >= 0))
  // the curve integrates to one over the range it shows (trapezoid), less the upper tail cut
  let area = 0
  for (let k = 1; k < answer.x.length; k++) area += (answer.y[k] + answer.y[k - 1]) / 2 * (answer.x[k] - answer.x[k - 1])
  assert.ok(Math.abs(area - (1 - answer.cutMass.above)) < 1e-3, `area ${area}`)
})

test('the correlation of an LKJ prior is closed form: 2 Beta(b, b) - 1, symmetric on (-1, 1)', () => {
  const p = plan({ id: 'e.correlation', kind: 'correlation', dist: 'lkj_corr_cholesky', k: 3, eta: 2 })
  const answer = bayesPriorPreview(p, 'e.correlation', 1)
  assert.equal(answer.kind, 'curve')
  assert.equal(answer.quantiles.q50, bayesBinding('lkj_corr_cholesky').quantile(0.5, [2, 3]))
  assert.ok(Math.abs(answer.quantiles.q50) < 1e-12)
  assert.ok(Math.abs(answer.quantiles.q5 + answer.quantiles.q95) < 1e-12)
  assert.ok(answer.x[0] >= -1 && answer.x.at(-1) <= 1)
})

test('a component and a state\'s initial value are marginals like a parameter', () => {
  const p = plan(
    { id: 'e.slope', kind: 'component', dist: 'normal', args: { mu: c(0), sigma: c(2) } },
    { id: 'level.init', kind: 'state_init', dist: 'normal', args: { mu: c(5), sigma: c(1) } },
  )
  assert.equal(bayesPriorPreview(p, 'e.slope', 1).kind, 'curve')
  assert.equal(bayesPriorPreview(p, 'level.init', 1).quantiles.q50, 5)
})

// ---------------------------------------------------------------- draws Stan would reject, truncation without mass, the cut

test('a scale that is not positive on some draws: those draws are left out, counted, the reason named', () => {
  // x ~ normal(0, a) with a ~ normal(0, 1): about half the draws have a negative scale
  const p = plan(
    param('a', 'normal', { mu: c(0), sigma: c(1) }),
    param('x', 'normal', { mu: c(0), sigma: { ref: 'a' } }),
  )
  const answer = bayesPriorPreview(p, 'x', SEED)
  assert.equal(answer.kind, 'histogram')
  assert.ok(Math.abs(answer.leftOut.count - 2000) < 160, `count ${answer.leftOut.count}`)
  assert.equal(answer.leftOut.reason, 'argument')
  assert.deepEqual(answer.leftOut.detail, { node: 'x', argument: 'sigma', constraint: 'positive' })
  assert.equal(answer.used, BAYES_PRIOR_DRAWS - answer.leftOut.count)
  assert.equal(answer.draws, BAYES_PRIOR_DRAWS)
  // nothing clipped or repaired: every kept draw is a draw of the model with a positive scale
  const draws = bayesDrawMarginals(p, SEED)
  let kept = 0
  for (let i = 0; i < draws.x.length; i++) {
    if (Number.isNaN(draws.x[i])) assert.ok(!(draws.a[i] > 0))
    else { kept++; assert.ok(draws.a[i] > 0) }
  }
  assert.equal(kept, answer.used)
})

test('an expression that is not finite on some draws: the log of a negative leaves the draws out, and what depends on them', () => {
  const p = plan(
    param('a', 'normal', { mu: c(0), sigma: c(1) }),
    { id: 'l', kind: 'transformed', value: ast(call('log', name('a'))) },
    { id: 'm', kind: 'transformed', value: ast(bin('*', num(2), name('l'))) },
  )
  const answer = bayesPriorPreview(p, 'm', SEED)
  assert.equal(answer.leftOut.reason, 'not_finite')
  assert.equal(answer.leftOut.detail.node, 'l')
  assert.ok(Math.abs(answer.leftOut.count - 2000) < 160)
  // a division by zero is not finite either
  const zero = plan(param('a', 'normal', { mu: c(0), sigma: c(1) }), { id: 'r', kind: 'transformed', value: ast(bin('/', num(1), bin('-', name('a'), name('a')))) })
  assert.equal(bayesPriorPreview(zero, 'r', 1).none, 'no_mass')
})

test('a truncation with no mass at some draws\' arguments: those draws are left out, others kept', () => {
  const p = plan(
    param('m', 'normal', { mu: c(0), sigma: c(10) }),
    param('y', 'normal', { mu: { ref: 'm' }, sigma: c(1) }, { range: { lower: 12 }, truncated: true }),
  )
  const answer = bayesPriorPreview(p, 'y', SEED)
  assert.equal(answer.leftOut.reason, 'no_mass')
  assert.ok(answer.leftOut.count > 0 && answer.used > 0)
  const draws = bayesDrawMarginals(p, SEED).y
  for (const x of draws) assert.ok(Number.isNaN(x) || x >= 12)
})

test('a truncation with no mass at all answers none, in the closed form and in the draws', () => {
  const closed = plan(param('y', 'normal', { mu: c(0), sigma: c(1) }, { range: { lower: 50 }, truncated: true }))
  assert.equal(bayesPriorPreview(closed, 'y', 1).none, 'no_mass')
  const drawn = plan(
    param('m', 'normal', { mu: c(0), sigma: c(1) }),
    param('y', 'normal', { mu: c(0), sigma: c(1) }, { range: { lower: 50 }, truncated: true }),
    param('z', 'normal', { mu: { ref: 'y' }, sigma: c(1) }),
  )
  const answer = bayesPriorPreview(drawn, 'z', 1)
  assert.equal(answer.none, 'no_mass')
  assert.equal(answer.leftOut.reason, 'no_mass')
})

test('heavy tails: the axis is cut at the 0.5 % quantiles and the draws beyond are counted', () => {
  for (const [dist, args] of [['cauchy', { mu: { ref: 'loc' }, sigma: c(1) }], ['student_t', { nu: c(1), mu: { ref: 'loc' }, sigma: c(1) }]]) {
    const p = plan(param('loc', 'normal', { mu: c(0), sigma: c(1) }), param('x', dist, args))
    const answer = bayesPriorPreview(p, 'x', SEED)
    assert.equal(answer.kind, 'histogram', dist)
    assert.ok(answer.cut.below >= 18 && answer.cut.below <= 22, `${dist} below ${answer.cut.below}`)
    assert.ok(answer.cut.above >= 18 && answer.cut.above <= 22, `${dist} above ${answer.cut.above}`)
    const span = answer.x.at(-1) - answer.x[0]
    assert.ok(Number.isFinite(span) && span < 1000, `${dist} span ${span}`)
  }
  const closed = plan(param('x', 'cauchy', { mu: c(0), sigma: c(1) }))
  const answer = bayesPriorPreview(closed, 'x', 1)
  assert.ok(Math.abs(answer.cutMass.below - 0.005) < 1e-12)
})

// ---------------------------------------------------------------- what is not drawn

test('a quantity that needs data, or has errors above it, is not drawn and says why', () => {
  const p = plan(param('mu', 'normal', { mu: c(0), sigma: c(1) }), param('bad', 'normal', { mu: c(0), sigma: c(1) }, { errors: 2 }),
    param('child', 'normal', { mu: { ref: 'bad' }, sigma: c(1) }))
  assert.equal(bayesPriorPreview(p, 'absent', 1).none, 'needs_data')
  // a read that is not in the plan (a data column, a linear predictor) is data
  const reading = plan(param('theta', 'normal', { mu: { ref: 'x_data' }, sigma: c(1) }))
  assert.equal(bayesPriorPreview(reading, 'theta', 1).none, 'needs_data')
  assert.equal(bayesPriorPreview(p, 'bad', 1).none, 'errors')
  assert.equal(bayesPriorPreview(p, 'child', 1).none, 'errors')
  assert.equal(bayesPriorPreview(p, 'mu', 1).kind, 'curve')
  // an incomplete prior and a constant that breaks its constraint are errors, not exceptions
  assert.equal(bayesPriorPreview(plan(param('w', 'normal', { mu: c(0) })), 'w', 1).none, 'errors')
  assert.equal(bayesPriorPreview(plan(param('w', 'normal', { mu: c(0), sigma: c(-1) })), 'w', 1).none, 'errors')
})

test('a transformed value that is the same on every draw says so', () => {
  const p = plan({ id: 't', kind: 'transformed', value: ast(bin('+', num(1), num(2))) })
  const answer = bayesPriorPreview(p, 't', 1)
  assert.equal(answer.none, 'constant')
  assert.equal(answer.quantiles.q50, 3)
})

// ---------------------------------------------------------------- expressions

test('expressions: IEEE arithmetic, ^ is Math.pow, the stable functions agree with their definitions', () => {
  const p = plan(
    param('a', 'normal', { mu: c(0), sigma: c(3) }),
    { id: 'e1', kind: 'transformed', value: ast(bin('^', call('square', name('a')), num(0.5))) },
    { id: 'e2', kind: 'transformed', value: ast({ kind: 'neg', args: [call('log1p_exp', name('a'))] }) },
    { id: 'e3', kind: 'transformed', value: ast(call('log_inv_logit', name('a'))) },
    { id: 'e4', kind: 'transformed', value: ast(call('logit', call('inv_logit', name('a')))) },
    { id: 'e5', kind: 'transformed', value: ast(bin('-', call('expm1', name('a')), bin('-', call('exp', name('a')), num(1)))) },
  )
  const d = bayesDrawMarginals(p, 3)
  for (let i = 0; i < 200; i++) {
    const a = d.a[i]
    assert.equal(d.e1[i], Math.pow(a * a, 0.5))
    assert.ok(Math.abs(d.e2[i] + Math.log(1 + Math.exp(a))) < 1e-12)
    assert.ok(Math.abs(d.e3[i] - Math.log(1 / (1 + Math.exp(-a)))) < 1e-12)
    assert.ok(Math.abs(d.e4[i] - a) < 1e-9)
    assert.ok(Math.abs(d.e5[i]) < 1e-12)
  }
  // far in the tails neither overflows
  const wide = plan(param('a', 'normal', { mu: c(800), sigma: c(1) }), { id: 'l', kind: 'transformed', value: ast(call('log1p_exp', name('a'))) },
    { id: 'p', kind: 'transformed', value: ast(call('inv_logit', ast_neg(name('a')))) })
  const w = bayesDrawMarginals(wide, 3)
  assert.ok(w.l.every(x => Number.isFinite(x)) && w.p.every(x => x >= 0 && x < 1e-300))
})
function ast_neg (x) { return { kind: 'neg', args: [x] } }

test('a lag is never in a prior: the answer is none, not an exception', () => {
  const p = plan(param('a', 'normal', { mu: ast({ kind: 'lag', id: 'a', k: 1 }), sigma: c(1) }))
  const quiet = console.error
  console.error = () => {}
  try { assert.equal(bayesPriorPreview(p, 'a', 1).none, 'errors') } finally { console.error = quiet }
})

// ---------------------------------------------------------------- determinism, pools, shapes

test('the same plan and seed give the same arrays, also after another plan ran in between; another seed gives other draws', () => {
  const first = bayesDrawMarginals(planNormalNormal, 5)
  bayesDrawMarginals(planStudent, 77)
  bayesPriorPreview(planSchools, 'school', 3)
  const again = bayesDrawMarginals(planNormalNormal, 5)
  assert.deepEqual(again.x, first.x)
  assert.deepEqual(again.mu, first.mu)
  const other = bayesDrawMarginals(planNormalNormal, 6)
  assert.notDeepEqual(other.x, first.x)
  const a = bayesPriorPreview(planStudent, 'x', 5)
  bayesPriorPreview(planNormalNormal, 'x', 9)
  assert.deepEqual(bayesPriorPreview(planStudent, 'x', 5).y, a.y)
})

test('the pools are allocated once and stay within the limits', () => {
  const pool = bayesPriorPool(BAYES_PRIOR_DRAWS)
  assert.deepEqual(pool.allocated(), { nodes: 0, expressions: 0 })
  pool.node(2)
  assert.equal(pool.allocated().nodes, 3)
  assert.throws(() => pool.node(200), /more than 200 nodes/)
  const taken = []
  for (let i = 0; i < 256; i++) taken.push(pool.take())
  assert.throws(() => pool.take(), /more than 256/)
  pool.give(taken.pop())
  assert.doesNotThrow(() => pool.take())
  // a deep expression reuses a few temporaries, whatever its size
  let deep = name('a')
  for (let i = 0; i < 100; i++) deep = bin('+', bin('*', deep, num(1)), bin('-', name('a'), name('a')))
  const p = plan(param('a', 'normal', { mu: c(0), sigma: c(1) }), { id: 'd', kind: 'transformed', value: ast(deep) })
  const d = bayesDrawMarginals(p, 1)
  assert.deepEqual(d.d, d.a)
})

test('the answers\' shapes', () => {
  const curve = bayesPriorPreview(plan(param('t', 'normal', { mu: c(0), sigma: c(1) })), 't', 1)
  assert.deepEqual(Object.keys(curve).sort(), ['cutMass', 'kind', 'mixture', 'ms', 'quantiles', 'target', 'truncated', 'x', 'y'])
  assert.ok(curve.x instanceof Float64Array && curve.y instanceof Float64Array)
  const hist = bayesPriorPreview(planNormalNormal, 'x', 1)
  assert.deepEqual(Object.keys(hist).sort(), ['cut', 'draws', 'kind', 'mixture', 'ms', 'quantiles', 'target', 'truncated', 'used', 'x', 'y'])
  assert.ok(hist.x instanceof Float64Array && hist.y instanceof Float64Array)
  assert.equal(hist.draws, 4000)
  assert.ok(hist.ms >= 0 && curve.ms >= 0)
  const none = bayesPriorPreview(planNormalNormal, 'nothing', 1)
  assert.deepEqual(Object.keys(none).sort(), ['kind', 'ms', 'none', 'target'])
  assert.equal(none.kind, 'none')
})

test('response times in Node (measured and recorded, never asserted)', (t) => {
  // the spec's chain: mu ~ normal(0, 10); tau_d ~ normal(0, 5) cut at 0; mu_d ~ normal(mu, tau_d); tau_s ~ exponential(1); alpha ~ normal(mu_d, tau_s)
  const chain = plan(
    param('mu', 'normal', { mu: c(0), sigma: c(10) }),
    param('tau_d', 'normal', { mu: c(0), sigma: c(5) }, halfBounds),
    param('mu_d', 'normal', { mu: { ref: 'mu' }, sigma: { ref: 'tau_d' } }),
    param('tau_s', 'exponential', { beta: c(1) }),
    param('alpha', 'normal', { mu: { ref: 'mu_d' }, sigma: { ref: 'tau_s' } }),
  )
  const cold = bayesPriorPreview(chain, 'alpha', 1)
  assert.equal(cold.kind, 'histogram')
  const times = []
  for (let i = 0; i < 30; i++) times.push(bayesPriorPreview(chain, 'alpha', i + 2).ms)
  times.sort((a, b) => a - b)
  const curve = bayesPriorPreview(plan(param('t', 'normal', { mu: c(0), sigma: c(1) })), 't', 1)
  t.diagnostic(`three-level chain, 4000 draws: first call ${cold.ms.toFixed(1)} ms, median of 30 ${((times[14] + times[15]) / 2).toFixed(2)} ms, max ${times[29].toFixed(1)} ms; closed-form curve ${curve.ms.toFixed(2)} ms`)
})
