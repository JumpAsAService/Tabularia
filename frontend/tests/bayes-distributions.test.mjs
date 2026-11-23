// The browser's bindings of the catalogue's distributions (composables/bayesDistributions.ts):
// density, log density, CDF and quantile against the witness's points (SciPy, outside Tabularia:
// gateway-go/internal/bayescatalog/testdata/points.json), against closed forms where one exists,
// the Student-t's location and scale, seeded draws (the same seed, the same draws, also after a
// saved state is restored), truncated draws by inversion (inside the bounds, and their
// empirical CDF against the truncated CDF by a Kolmogorov-Smirnov test with its p-value against
// the suite's alpha), and the LKJ correlation's moments.
// Small data always: points and a few thousand draws, never a large table.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const { bayesBinding, bayesStream } = await import('~/composables/bayesDistributions.ts')

const ALPHA = 0.001 // the suite's alpha; the seeds are fixed, so the verdict is deterministic

function close (actual, expected, relative, label) {
  if (expected === 0) return assert.ok(Math.abs(actual) <= relative, `${label}: ${actual} is not 0`)
  assert.ok(Math.abs(actual - expected) <= relative * Math.abs(expected), `${label}: ${actual} vs ${expected}`)
}

// Kolmogorov distribution's survival function, the series 2·Σ (−1)^(k−1) exp(−2 k² λ²)
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

const POINTS = fileURLToPath(new URL('../../gateway-go/internal/bayescatalog/testdata/points.json', import.meta.url))

test('the witness points: pdf, logpdf and cdf within 1e-10, quantile within 1e-8', (t) => {
  if (!existsSync(POINTS)) return t.skip('gateway-go/internal/bayescatalog/testdata/points.json does not exist yet (lot C2 writes it)')
  const { cases } = JSON.parse(readFileSync(POINTS, 'utf8'))
  let compared = 0
  const failures = []
  for (const c of cases) {
    const binding = bayesBinding(c.dist)
    if (!binding) continue // the likelihood-only distributions are not drawn in the browser
    // the two marginals take the one correlation's / the one component's arguments
    let args = c.args
    if (c.dist === 'lkj_corr_cholesky') args = [c.args[0], c.dim]
    if (c.dist === 'multi_normal_cholesky') args = [c.args[0][c.k], Math.hypot(...c.args[1][c.k])]
    const label = `${c.dist}(${JSON.stringify(c.args)}) at ${c.x}`
    try {
      close(binding.logpdf(c.x, args), c.logpdf, 1e-10, `${label} logpdf`)
      if (c.pdf !== undefined) close(binding.pdf(c.x, args), c.pdf, 1e-10, `${label} pdf`)
      if (c.cdf !== undefined) close(binding.cdf(c.x, args), c.cdf, 1e-10, `${label} cdf`)
      if (c.p !== null && c.p !== undefined) close(binding.quantile(c.p, args), c.x, 1e-8, `${label} quantile(${c.p})`)
      compared++
    } catch (e) {
      failures.push(e.message)
    }
  }
  assert.ok(compared > 100, `only ${compared} points compared`)
  assert.deepEqual(failures, [])
})

test('closed forms the library must agree with', () => {
  const cauchy = bayesBinding('cauchy')
  close(cauchy.pdf(1, [0, 2]), 1 / (2 * Math.PI * (1 + 1 / 4)), 1e-13, 'cauchy pdf')
  close(bayesBinding('exponential').pdf(1, [2]), 2 * Math.exp(-2), 1e-13, 'exponential pdf')
  close(bayesBinding('gamma').pdf(1, [2, 3]), 9 * Math.exp(-3), 1e-13, 'gamma pdf (shape, rate)')
  close(bayesBinding('inv_gamma').pdf(1, [2, 3]), 9 * Math.exp(-3), 1e-13, 'inverse gamma pdf (shape, scale)')
  close(bayesBinding('std_normal').pdf(0, []), 1 / Math.sqrt(2 * Math.PI), 1e-13, 'std_normal pdf')
  close(bayesBinding('uniform').pdf(0.5, [0, 2]), 0.5, 1e-13, 'uniform pdf')
  assert.equal(bayesBinding('uniform').pdf(3, [0, 2]), 0)
  close(bayesBinding('lognormal').quantile(0.5, [1, 0.5]), Math.E, 1e-12, 'lognormal median')
})

test("the Student-t is moved to mu and stretched to sigma", () => {
  const t = bayesBinding('student_t')
  // nu = 1 is the Cauchy with the same location and scale
  const cauchy = bayesBinding('cauchy')
  for (const x of [-3, 0.5, 2, 7.25]) {
    close(t.pdf(x, [1, 1.5, 2]), cauchy.pdf(x, [1.5, 2]), 1e-12, `pdf at ${x}`)
    close(t.logpdf(x, [1, 1.5, 2]), cauchy.logpdf(x, [1.5, 2]), 1e-12, `logpdf at ${x}`)
    close(t.cdf(x, [1, 1.5, 2]), cauchy.cdf(x, [1.5, 2]), 1e-12, `cdf at ${x}`)
  }
  // nu = 2: F(z) = 1/2 + z / (2·sqrt(2 + z²)) at z = (x − mu)/sigma
  const z = (4 - 1) / 3
  close(t.cdf(4, [2, 1, 3]), 0.5 + z / (2 * Math.sqrt(2 + z * z)), 1e-12, 'nu = 2 cdf')
  close(t.quantile(t.cdf(4, [2, 1, 3]), [2, 1, 3]), 4, 1e-9, 'quantile inverts cdf')
  assert.equal(t.quantile(0.5, [5, -2, 4]), -2)
  // the density integrates to 1 over a wide range (trapezoid on 40,001 points)
  let area = 0
  const lo = -2000; const hi = 2000; const n = 40000; const h = (hi - lo) / n
  for (let i = 0; i <= n; i++) area += t.pdf(lo + i * h, [30, 0, 2]) * (i === 0 || i === n ? h / 2 : h)
  close(area, 1, 1e-6, 'area')
})

test('arguments outside their constraint give NaN, never an exception', () => {
  assert.ok(Number.isNaN(bayesBinding('normal').pdf(0, [0, -1])))
  assert.ok(Number.isNaN(bayesBinding('student_t').pdf(0, [1, 0, 0])))
  assert.ok(Number.isNaN(bayesBinding('lkj_corr_cholesky').pdf(0, [2, 1])))
  const out = new Float64Array(4)
  bayesBinding('gamma').draw(bayesStream(1).uniform, [-1, 1], out)
  assert.ok(out.every(Number.isNaN))
})

test('the same seed gives the same draws, also after a saved state is restored', () => {
  const draws = (stream, id, args, n = 50) => {
    const out = new Float64Array(n)
    bayesBinding(id).draw(stream.uniform, args, out)
    return Array.from(out)
  }
  const cases = [['normal', [0, 1]], ['student_t', [3, 1, 2]], ['gamma', [0.5, 2]], ['beta', [50, 2]], ['uniform', [-1, 1]], ['lkj_corr_cholesky', [2, 3]]]
  for (const [id, args] of cases) {
    assert.deepEqual(draws(bayesStream(12345), id, args), draws(bayesStream(12345), id, args), `${id}: same seed`)
    assert.notDeepEqual(draws(bayesStream(12345), id, args), draws(bayesStream(54321), id, args), `${id}: other seed`)
  }
  const stream = bayesStream(7)
  const state = stream.getState()
  const first = draws(stream, 'gamma', [2, 3])
  // other distributions drawing from the same stream move it; restoring brings the first draws back
  draws(stream, 'normal', [0, 1], 10)
  stream.setState(state)
  assert.deepEqual(draws(stream, 'gamma', [2, 3]), first)
  // a seed of 0 is taken as 1, and a negative or fractional one as its whole absolute value
  assert.deepEqual(draws(bayesStream(0), 'normal', [0, 1], 5), draws(bayesStream(1), 'normal', [0, 1], 5))
})

test('untruncated draws follow their distribution (Kolmogorov-Smirnov against the alpha)', () => {
  const cases = [['normal', [1, 2]], ['student_t', [4, -1, 0.5]], ['cauchy', [0, 1]], ['exponential', [2]], ['gamma', [0.5, 2]],
    ['inv_gamma', [3, 2]], ['lognormal', [0, 0.5]], ['beta', [0.5, 0.5]], ['uniform', [-2, 5]], ['lkj_corr_cholesky', [1, 4]]]
  for (const [id, args] of cases) {
    const binding = bayesBinding(id)
    const out = new Float64Array(2000)
    assert.equal(binding.draw(bayesStream(2024).uniform, args, out), out.length)
    const p = ksPValue(Array.from(out).sort((a, b) => a - b), x => binding.cdf(x, args))
    assert.ok(p > ALPHA, `${id}: Kolmogorov-Smirnov p-value ${p} is not above ${ALPHA}`)
  }
})

test('truncated draws stay inside the bounds and follow the truncated CDF', () => {
  const cases = [
    ['normal', [0, 1], 0, undefined], // the half-normal
    ['normal', [0, 1], -1, 2],
    ['student_t', [3, 0, 1], undefined, 1.5],
    ['gamma', [2, 1], 1, 4],
    ['beta', [2, 5], 0.1, 0.4],
    ['lognormal', [0, 1], undefined, 3],
    ['cauchy', [0, 1], 1, undefined],
  ]
  for (const [id, args, lower, upper] of cases) {
    const binding = bayesBinding(id)
    const out = new Float64Array(2000)
    const written = binding.draw(bayesStream(99).uniform, args, out, lower, upper)
    assert.equal(written, out.length, `${id}: all written`)
    for (const x of out) assert.ok((lower === undefined || x >= lower) && (upper === undefined || x <= upper), `${id}: ${x} outside [${lower}, ${upper}]`)
    const atLow = lower === undefined ? 0 : binding.cdf(lower, args)
    const atHigh = upper === undefined ? 1 : binding.cdf(upper, args)
    const p = ksPValue(Array.from(out).sort((a, b) => a - b), x => (binding.cdf(x, args) - atLow) / (atHigh - atLow))
    assert.ok(p > ALPHA, `${id} truncated to [${lower}, ${upper}]: p-value ${p} is not above ${ALPHA}`)
  }
})

test('bounds that leave no representable mass draw nothing', () => {
  const out = new Float64Array(10).fill(-7)
  assert.equal(bayesBinding('normal').draw(bayesStream(1).uniform, [0, 1], out, 50, 60), 0)
  assert.ok(out.every(x => x === -7), 'nothing was written')
  assert.equal(bayesBinding('normal').draw(bayesStream(1).uniform, [0, 1], out, 1, 1), 0)
})

test('the LKJ correlation has mean 0 and variance 1/(2·eta + K − 1)', () => {
  const lkj = bayesBinding('lkj_corr_cholesky')
  for (const [eta, size] of [[1, 3], [2, 4], [0.5, 2], [5, 10]]) {
    const n = 20000
    const out = new Float64Array(n)
    lkj.draw(bayesStream(31).uniform, [eta, size], out)
    let sum = 0
    for (const x of out) sum += x
    const mean = sum / n
    let squares = 0
    for (const x of out) squares += (x - mean) ** 2
    const variance = squares / (n - 1)
    const expected = 1 / (2 * eta + size - 1)
    // within five Monte Carlo standard errors of the fixed draws
    assert.ok(Math.abs(mean) < 5 * Math.sqrt(expected / n), `eta ${eta}, K ${size}: mean ${mean}`)
    assert.ok(Math.abs(variance - expected) < 5 * expected * Math.sqrt(2 / n) * 1.5, `eta ${eta}, K ${size}: variance ${variance} vs ${expected}`)
    for (const x of out) assert.ok(x > -1 && x < 1)
  }
})
