// The browser's bindings of the catalogue's distributions: density, log density, CDF, quantile and
// seeded draws, through the stdlib packages (exact versions in package.json). What a distribution
// is called and how Stan parametrizes it is the catalogue's (composables/bayesCatalog.ts); this
// file only says how each one's Stan arguments reach the library's.
//
// IMPORTED ONLY BY THE WORKER OF THE PRIOR PREVIEW AND BY THE TESTS: the packages weigh about
// 100 KB gzipped and must never land in the entry chunk. Nothing here is imported by a module
// the entry loads, and nothing of Nuxt or the DOM is used.
//
// Every export starts with `bayes` or `Bayes`: a composable's exports are auto-imported
// everywhere, so no other name may leak.
//
// Arguments are Stan's, in Stan's order. A binding returns NaN for arguments outside their
// constraint (the library does) and never throws; the caller checks the constraints first.
// Draws go into an array the caller sized: nothing here grows an array.

import normalPdf from '@stdlib/stats-base-dists-normal-pdf'
import normalLogpdf from '@stdlib/stats-base-dists-normal-logpdf'
import normalCdf from '@stdlib/stats-base-dists-normal-cdf'
import normalQuantile from '@stdlib/stats-base-dists-normal-quantile'
import normalRandom from '@stdlib/random-base-normal'
import tPdf from '@stdlib/stats-base-dists-t-pdf'
import tLogpdf from '@stdlib/stats-base-dists-t-logpdf'
import tCdf from '@stdlib/stats-base-dists-t-cdf'
import tQuantile from '@stdlib/stats-base-dists-t-quantile'
import tRandom from '@stdlib/random-base-t'
import cauchyPdf from '@stdlib/stats-base-dists-cauchy-pdf'
import cauchyLogpdf from '@stdlib/stats-base-dists-cauchy-logpdf'
import cauchyCdf from '@stdlib/stats-base-dists-cauchy-cdf'
import cauchyQuantile from '@stdlib/stats-base-dists-cauchy-quantile'
import cauchyRandom from '@stdlib/random-base-cauchy'
import exponentialPdf from '@stdlib/stats-base-dists-exponential-pdf'
import exponentialLogpdf from '@stdlib/stats-base-dists-exponential-logpdf'
import exponentialCdf from '@stdlib/stats-base-dists-exponential-cdf'
import exponentialQuantile from '@stdlib/stats-base-dists-exponential-quantile'
import exponentialRandom from '@stdlib/random-base-exponential'
import gammaPdf from '@stdlib/stats-base-dists-gamma-pdf'
import gammaLogpdf from '@stdlib/stats-base-dists-gamma-logpdf'
import gammaCdf from '@stdlib/stats-base-dists-gamma-cdf'
import gammaQuantile from '@stdlib/stats-base-dists-gamma-quantile'
import gammaRandom from '@stdlib/random-base-gamma'
import invgammaPdf from '@stdlib/stats-base-dists-invgamma-pdf'
import invgammaLogpdf from '@stdlib/stats-base-dists-invgamma-logpdf'
import invgammaCdf from '@stdlib/stats-base-dists-invgamma-cdf'
import invgammaQuantile from '@stdlib/stats-base-dists-invgamma-quantile'
import invgammaRandom from '@stdlib/random-base-invgamma'
import lognormalPdf from '@stdlib/stats-base-dists-lognormal-pdf'
import lognormalLogpdf from '@stdlib/stats-base-dists-lognormal-logpdf'
import lognormalCdf from '@stdlib/stats-base-dists-lognormal-cdf'
import lognormalQuantile from '@stdlib/stats-base-dists-lognormal-quantile'
import lognormalRandom from '@stdlib/random-base-lognormal'
import betaPdf from '@stdlib/stats-base-dists-beta-pdf'
import betaLogpdf from '@stdlib/stats-base-dists-beta-logpdf'
import betaCdf from '@stdlib/stats-base-dists-beta-cdf'
import betaQuantile from '@stdlib/stats-base-dists-beta-quantile'
import betaRandom from '@stdlib/random-base-beta'
import uniformPdf from '@stdlib/stats-base-dists-uniform-pdf'
import uniformLogpdf from '@stdlib/stats-base-dists-uniform-logpdf'
import uniformCdf from '@stdlib/stats-base-dists-uniform-cdf'
import uniformQuantile from '@stdlib/stats-base-dists-uniform-quantile'
import uniformRandom from '@stdlib/random-base-uniform'

/** A stream of uniform numbers on [0, 1): what every generator of the library takes as its `prng`. */
export type BayesUniform = () => number

export interface BayesBinding {
  pdf(x: number, args: number[]): number
  logpdf(x: number, args: number[]): number
  cdf(x: number, args: number[]): number
  quantile(p: number, args: number[]): number
  /**
   * Fills `out` with draws from the stream `rng` (seeded by the caller), truncated to
   * [lower, upper] when a bound is given (by inversion of the CDF: the bounds' rule of the
   * catalogue). Returns how many it wrote: `out.length`, or 0 when the bounds leave the
   * distribution no mass a double can represent (the caller says so; nothing is drawn).
   */
  draw(rng: BayesUniform, args: number[], out: Float64Array, lower?: number, upper?: number): number
}

/** One seeded stream, whose state can be saved and restored (the same state, the same draws again). */
export interface BayesStream {
  uniform: BayesUniform
  getState(): Uint32Array
  setState(state: Uint32Array): void
}

/**
 * The one stream of a preview: a MT19937 seeded with `seed` (a positive whole number; 0 is taken
 * as 1). The library's generators share it through their `prng` option, so the same seed gives
 * the same chart in every browser.
 */
export function bayesStream (seed: number): BayesStream {
  const whole = Math.floor(Math.abs(seed)) >>> 0
  // the state's owner: any generator's factory made with a seed owns its own MT19937
  const owner = normalRandom.factory({ seed: whole === 0 ? 1 : whole }) as unknown as {
    PRNG: BayesUniform
    state: Uint32Array
  }
  return {
    uniform: owner.PRNG,
    getState: () => new Uint32Array(owner.state),
    setState: (state) => { owner.state = state },
  }
}

// The library's functions, all `(x, ...parameters)`; its types fix the arity, ours do not.
type Fn = (x: number, ...parameters: number[]) => number
type Factory = (...parameters: any[]) => () => number

interface Family {
  pdf: Fn
  logpdf: Fn
  cdf: Fn
  quantile: Fn
  /** the generator's factory: (…parameters, { prng }) */
  random: Factory
}

/** How Stan's arguments become the library's: the parameters in its order, and the affine map x = shift + scale·y. */
interface Mapping {
  family: Family
  parameters: number[]
  shift: number
  scale: number
}

function family (pdf: unknown, logpdf: unknown, cdf: unknown, quantile: unknown, random: { factory: unknown }): Family {
  return {
    pdf: pdf as Fn, logpdf: logpdf as Fn, cdf: cdf as Fn, quantile: quantile as Fn,
    random: random.factory as Factory,
  }
}

const NORMAL = family(normalPdf, normalLogpdf, normalCdf, normalQuantile, normalRandom)
const STUDENT = family(tPdf, tLogpdf, tCdf, tQuantile, tRandom)
const CAUCHY = family(cauchyPdf, cauchyLogpdf, cauchyCdf, cauchyQuantile, cauchyRandom)
const EXPONENTIAL = family(exponentialPdf, exponentialLogpdf, exponentialCdf, exponentialQuantile, exponentialRandom)
const GAMMA = family(gammaPdf, gammaLogpdf, gammaCdf, gammaQuantile, gammaRandom)
const INVGAMMA = family(invgammaPdf, invgammaLogpdf, invgammaCdf, invgammaQuantile, invgammaRandom)
const LOGNORMAL = family(lognormalPdf, lognormalLogpdf, lognormalCdf, lognormalQuantile, lognormalRandom)
const BETA = family(betaPdf, betaLogpdf, betaCdf, betaQuantile, betaRandom)
const UNIFORM = family(uniformPdf, uniformLogpdf, uniformCdf, uniformQuantile, uniformRandom)

const direct = (f: Family, parameters: number[]): Mapping => ({ family: f, parameters, shift: 0, scale: 1 })

/** A mapping that nothing satisfies: every answer is NaN. */
const NOWHERE: Mapping = { family: NORMAL, parameters: [Number.NaN, Number.NaN], shift: 0, scale: 1 }

/**
 * A binding from a rule that maps Stan's arguments to the library's. With `scale` ≠ 1 the
 * variable is stretched and moved: x = shift + scale·y, density f((x − shift)/scale)/scale.
 */
function binding (map: (args: number[]) => Mapping): BayesBinding {
  const checked = (args: number[]): Mapping => {
    const m = map(args)
    return m.scale > 0 && Number.isFinite(m.shift) && Number.isFinite(m.scale) ? m : NOWHERE
  }
  const cdf = (x: number, args: number[]): number => {
    const m = checked(args)
    return m.family.cdf((x - m.shift) / m.scale, ...m.parameters)
  }
  const quantile = (p: number, args: number[]): number => {
    const m = checked(args)
    return m.shift + m.scale * m.family.quantile(p, ...m.parameters)
  }
  return {
    pdf (x, args) {
      const m = checked(args)
      return m.family.pdf((x - m.shift) / m.scale, ...m.parameters) / m.scale
    },
    logpdf (x, args) {
      const m = checked(args)
      return m.family.logpdf((x - m.shift) / m.scale, ...m.parameters) - Math.log(m.scale)
    },
    cdf,
    quantile,
    draw (rng, args, out, lower, upper) {
      const m = checked(args)
      if (lower === undefined && upper === undefined) {
        let next: () => number
        try {
          next = m.family.random(...m.parameters, { prng: rng })
        } catch {
          // a generator's factory refuses some arguments the other functions answer NaN to
          out.fill(Number.NaN)
          return out.length
        }
        for (let i = 0; i < out.length; i++) out[i] = m.shift + m.scale * next()
        return out.length
      }
      // truncation by inversion: u uniform on (F(lower), F(upper)), x = Q(u)
      const low = lower ?? Number.NEGATIVE_INFINITY
      const high = upper ?? Number.POSITIVE_INFINITY
      const atLow = low === Number.NEGATIVE_INFINITY ? 0 : cdf(low, args)
      const atHigh = high === Number.POSITIVE_INFINITY ? 1 : cdf(high, args)
      const mass = atHigh - atLow
      if (!(mass > 0)) return 0
      for (let i = 0; i < out.length; i++) {
        // a uniform of exactly 0 would ask for the quantile of 0 (minus infinity for a real line)
        const u = Math.max(rng(), Number.MIN_VALUE)
        const x = quantile(Math.min(atLow + u * mass, 1 - Number.EPSILON / 2), args)
        out[i] = Math.min(Math.max(x, low), high)
      }
      return out.length
    },
  }
}

/** The correlation of an LKJ prior on a K×K matrix: (r + 1)/2 ~ Beta(eta − 1 + K/2, eta − 1 + K/2). */
function lkjMarginal (args: number[]): Mapping {
  const [eta, size] = args
  const b = eta - 1 + size / 2
  return size >= 2 && eta > 0 ? { family: BETA, parameters: [b, b], shift: -1, scale: 2 } : NOWHERE
}

/**
 * The bindings by the catalogue's distribution id. The arguments are Stan's, in Stan's order,
 * except for the two marginals: `lkj_corr_cholesky` takes [eta, K] (the matrix's size) and
 * describes one correlation; `multi_normal_cholesky` takes [mu_k, tau_k] and describes one
 * component. The distributions the browser does not draw (`browser: "none"`) have no entry.
 */
export const BAYES_BINDINGS: Readonly<Record<string, BayesBinding>> = {
  normal: binding(a => direct(NORMAL, [a[0], a[1]])),
  // stdlib's t is the standard one (degrees of freedom only): moved to mu and stretched to sigma
  student_t: binding(a => ({ family: STUDENT, parameters: [a[0]], shift: a[1], scale: a[2] })),
  cauchy: binding(a => direct(CAUCHY, [a[0], a[1]])),
  // Stan's beta is the rate, as the library's lambda
  exponential: binding(a => direct(EXPONENTIAL, [a[0]])),
  // shape and rate, in both
  gamma: binding(a => direct(GAMMA, [a[0], a[1]])),
  // shape and scale, in both
  inv_gamma: binding(a => direct(INVGAMMA, [a[0], a[1]])),
  lognormal: binding(a => direct(LOGNORMAL, [a[0], a[1]])),
  beta: binding(a => direct(BETA, [a[0], a[1]])),
  uniform: binding(a => direct(UNIFORM, [a[0], a[1]])),
  lkj_corr_cholesky: binding(lkjMarginal),
  // with L = diag(tau)·L_omega, component k is normal(mu_k, tau_k) whatever the correlation
  multi_normal_cholesky: binding(a => direct(NORMAL, [a[0], a[1]])),
  std_normal: binding(() => direct(NORMAL, [0, 1])),
}

export function bayesBinding (id: string): BayesBinding | undefined {
  return Object.hasOwn(BAYES_BINDINGS, id) ? BAYES_BINDINGS[id] : undefined
}
