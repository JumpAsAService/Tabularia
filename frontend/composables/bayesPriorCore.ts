// The core of the priors' preview: what one quantity's prior implies, computed from the graph's
// prior plan (docs/specs/bayes-prior-preview.md). When every argument of the prior is a constant the
// answer is exact (the density's curve and the quantiles, no draw); when an argument depends on a
// parameter or a transformed value the answer is the true marginal by ancestral sampling: the
// parents are drawn first, then the quantity, 4,000 times, from one seeded stream.
//
// The worker runs this file and the tests import it as it is: nothing of Nuxt, the DOM or a worker
// is used. It reads the distributions through composables/bayesDistributions.ts (stdlib) and must
// be imported only by the preview's worker and by the tests, never by the entry chunk.
//
// Memory (the rule of the small): every node's draws live in one Float64Array of a pool allocated
// on first use, and an expression is evaluated into temporary arrays of the same pool, at most
// `max_expression_nodes` of them. Nothing is allocated per draw.
//
// Every export starts with `bayes` or `Bayes`: a composable's exports are auto-imported everywhere.

import { bayesDistribution, bayesInterval, bayesIntervalContains, bayesLimit } from '~/composables/bayesCatalog'
import { bayesBinding, bayesStream } from '~/composables/bayesDistributions'
import type { BayesPlanAst, BayesPlanNode, BayesPlanSlot, BayesPriorPlan } from '~/composables/bayesGraph'

/** Draws a preview makes (a constant of the module: the spec's «Draws a preview»). */
export const BAYES_PRIOR_DRAWS = 4000
/** The curve's points and the histogram's bins. */
export const BAYES_PRIOR_CURVE_POINTS = 200
export const BAYES_PRIOR_BINS = 60
/** The axis is cut at these quantiles; a bound of the range closer than this share of the axis is kept. */
const CUT_BELOW = 0.005
const CUT_ABOVE = 0.995
const BOUND_SHARE = 0.1

/** Why a draw was left out: Stan would reject it, or no number can be drawn. */
export type BayesPriorLeftOutReason =
  /** an argument broke its constraint (a scale that is not positive): `detail` names the node, the argument and the constraint */
  | 'argument'
  /** an expression or a draw is not a finite number (the log of a negative, a division by 0) */
  | 'not_finite'
  /** a truncation with no probability left on the range, at this draw's arguments */
  | 'no_mass'

export interface BayesPriorAnswer {
  target: string
  /** a closed-form `curve`, the `histogram` of ancestral draws, or `none` with `none` saying why */
  kind: 'curve' | 'histogram' | 'none'
  /** the curve's points, or the histogram's bin edges (one more than the counts) */
  x?: Float64Array
  /** the density at each point, or the counts of the bins */
  y?: Float64Array
  quantiles?: { q5: number, q50: number, q95: number }
  /** histogram: the draws beyond each end of the axis, of the `used` ones */
  cut?: { below: number, above: number }
  /** curve: the probability beyond each end of the axis (the cut is the 0.5 % tails) */
  cutMass?: { below: number, above: number }
  /** the draws Stan would reject, counted over the target's own draws (those that depend on a rejected one included) */
  leftOut?: { count: number, reason: BayesPriorLeftOutReason, detail: { node: string, argument?: string, constraint?: string } }
  /** draws made, and the draws the quantiles and the histogram use (made minus left out) */
  draws?: number
  used?: number
  /** true when the answer is a mixture (an argument depends on a draw) */
  mixture?: boolean
  /** true when the prior is cut to a range narrower than its support */
  truncated?: boolean
  /**
   * `needs_data`: the quantity or one it reads is not in the plan (it needs data); `errors`: the
   * model has findings above it; `no_mass`: no draw is left (the truncation has no mass, or every
   * draw was rejected); `constant`: every draw is the same number (`quantiles` holds it)
   */
  none?: 'needs_data' | 'errors' | 'no_mass' | 'constant'
  /** the time the call took, for the measurements */
  ms: number
}

// ---------------------------------------------------------------- the pool

type Values = Float64Array | number

/** The arrays a preview draws into, allocated on first use and kept: per node, and for expressions. */
export interface BayesPriorPool {
  readonly size: number
  /** the array of the i-th node of an evaluation */
  node(index: number): Float64Array
  /** temporary arrays for expressions: taken and given back, never beyond `max_expression_nodes` */
  take(): Float64Array
  give(array: Float64Array): void
  /** the sorting space of the quantiles, `size` long */
  scratch: Float64Array
  /** how many arrays were ever allocated, for the tests of the pool's bounds */
  allocated(): { nodes: number, expressions: number }
}

export function bayesPriorPool (size: number = BAYES_PRIOR_DRAWS): BayesPriorPool {
  const nodes: Float64Array[] = []
  const free: Float64Array[] = []
  let expressions = 0
  const maxNodes = bayesLimit('max_nodes')
  const maxExpressions = bayesLimit('max_expression_nodes')
  return {
    size,
    scratch: new Float64Array(size),
    node (index) {
      if (index >= maxNodes) throw new Error(`prior preview: more than ${maxNodes} nodes`)
      while (nodes.length <= index) nodes.push(new Float64Array(size))
      return nodes[index]
    },
    take () {
      const reused = free.pop()
      if (reused) return reused
      if (expressions >= maxExpressions) throw new Error(`prior preview: more than ${maxExpressions} temporary arrays`)
      expressions++
      return new Float64Array(size)
    },
    give (array) { free.push(array) },
    allocated: () => ({ nodes: nodes.length, expressions }),
  }
}

let sharedPool: BayesPriorPool | null = null

function poolFor (size: number): BayesPriorPool {
  if (!sharedPool || sharedPool.size !== size) sharedPool = bayesPriorPool(size)
  return sharedPool
}

// ---------------------------------------------------------------- the expression language over arrays

/** log(1 + exp(x)) without overflow. */
function log1pExp (x: number): number {
  return x > 0 ? x + Math.log1p(Math.exp(-x)) : Math.log1p(Math.exp(x))
}

function invLogit (x: number): number {
  const e = Math.exp(-Math.abs(x))
  return x >= 0 ? 1 / (1 + e) : e / (1 + e)
}

/** The language's functions, by the catalogue's names; `lag` never reaches a prior. */
const FUNCTIONS: Record<string, (x: number) => number> = {
  exp: Math.exp,
  log: Math.log,
  log1p: Math.log1p,
  expm1: Math.expm1,
  sqrt: Math.sqrt,
  square: x => x * x,
  inv_logit: invLogit,
  logit: p => Math.log(p) - Math.log1p(-p),
  log1p_exp: log1pExp,
  log_inv_logit: x => -log1pExp(-x),
}

function binaryOp (op: string): (a: number, b: number) => number {
  switch (op) {
    case '+': return (a, b) => a + b
    case '-': return (a, b) => a - b
    case '*': return (a, b) => a * b
    case '/': return (a, b) => a / b
    case '^': return Math.pow
    default: throw new Error(`prior preview: unknown operator ${op}`)
  }
}

function functionOf (name: string): (x: number) => number {
  const f = Object.hasOwn(FUNCTIONS, name) ? FUNCTIONS[name] : undefined
  if (!f) throw new Error(`prior preview: ${name} has no meaning in a prior`)
  return f
}

/** The nodes an expression reads (`index` reads its target's representative unit). */
function astReads (ast: BayesPlanAst, into: Set<string>): void {
  switch (ast.kind) {
    case 'num': return
    case 'name': case 'index': case 'lag': into.add(ast.id); return
    case 'call': case 'neg': case 'binary': for (const a of ast.args) astReads(a, into)
  }
}

function slotReads (slot: BayesPlanSlot | undefined, into: Set<string>): void {
  if (!slot) return
  if ('ast' in slot) astReads(slot.ast, into)
  else if ('ref' in slot) into.add(slot.ref)
}

/** The value of an expression of numbers only (no name in it), or null when it reads a node. */
function constantOf (slot: BayesPlanSlot): number | null {
  if ('constant' in slot) return slot.constant
  if ('ref' in slot) return null
  const reads = new Set<string>()
  astReads(slot.ast, reads)
  if (reads.size > 0) return null
  const out = new Float64Array(1)
  const pool = { take: () => out, give: () => {} }
  const v = evalAst(slot.ast, () => out, pool as unknown as BayesPriorPool, 1)
  return typeof v === 'number' ? v : v[0]
}

/**
 * The expression's value on every draw: a number when it is the same on all of them, else an
 * array (a node's own, never to be written, or a temporary the caller gives back). Evaluation is
 * left to right with IEEE doubles and `Math`'s functions, the same in every browser.
 */
function evalAst (ast: BayesPlanAst, nodeValues: (id: string) => Float64Array, pool: BayesPriorPool, n: number): Values {
  switch (ast.kind) {
    case 'num': return ast.num
    // a plate's quantity is its representative unit, which an index points to as well
    case 'name': case 'index': return nodeValues(ast.id)
    case 'lag': throw new Error('prior preview: a lag is never in a prior')
    case 'neg': {
      const x = evalAst(ast.args[0], nodeValues, pool, n)
      if (typeof x === 'number') return -x
      const out = ownTemporary(x, pool)
      for (let i = 0; i < n; i++) out[i] = -x[i]
      return out
    }
    case 'call': {
      const f = functionOf(ast.name)
      const x = evalAst(ast.args[0], nodeValues, pool, n)
      if (typeof x === 'number') return f(x)
      const out = ownTemporary(x, pool)
      for (let i = 0; i < n; i++) out[i] = f(x[i])
      return out
    }
    case 'binary': {
      const op = binaryOp(ast.op)
      const a = evalAst(ast.args[0], nodeValues, pool, n)
      const b = evalAst(ast.args[1], nodeValues, pool, n)
      if (typeof a === 'number' && typeof b === 'number') return op(a, b)
      // the result goes into an operand's temporary when there is one, so the pool stays shallow
      const out = typeof a !== 'number' && isTemporary(a) ? a : typeof b !== 'number' && isTemporary(b) ? b : pool.take()
      markTemporary(out)
      for (let i = 0; i < n; i++) out[i] = op(typeof a === 'number' ? a : a[i], typeof b === 'number' ? b : b[i])
      if (typeof a !== 'number' && a !== out && isTemporary(a)) pool.give(a)
      if (typeof b !== 'number' && b !== out && isTemporary(b)) pool.give(b)
      return out
    }
  }
}

// The pool's temporaries are told from the nodes' own arrays by identity, so a node's draws are never overwritten.
const temporaries = new WeakSet<Float64Array>()
const isTemporary = (a: Float64Array): boolean => temporaries.has(a)
const markTemporary = (a: Float64Array): void => { temporaries.add(a) }

function ownTemporary (x: Float64Array, pool: BayesPriorPool): Float64Array {
  if (isTemporary(x)) return x
  const out = pool.take()
  markTemporary(out)
  return out
}

// ---------------------------------------------------------------- quantiles

/** Hyndman and Fan's type 7 (NumPy's default) on a sorted array's first `count` values. */
function quantileSorted (sorted: Float64Array, count: number, p: number): number {
  const h = (count - 1) * p
  const low = Math.floor(h)
  const high = Math.min(low + 1, count - 1)
  return sorted[low] + (h - low) * (sorted[high] - sorted[low])
}

// ---------------------------------------------------------------- the plan's nodes

interface DrawnNode {
  node: BayesPlanNode
  /** where its draws are */
  values: Float64Array
  /** the nodes it reads */
  reads: string[]
}

interface Cause { reason: BayesPriorLeftOutReason, node: string, argument?: string, constraint?: string, count: number }

/** What a node's prior says: the distribution's id, its arguments' slots in the binding's order and their names. */
interface PriorSpec {
  dist: string
  slots: BayesPlanSlot[]
  names: string[]
  constraints: string[]
}

/** The node's prior as the binding takes it, or null when the plan does not have all of it. */
function priorSpec (node: BayesPlanNode): PriorSpec | null {
  if (!node.dist || !bayesBinding(node.dist)) return null
  if (node.kind === 'correlation') {
    if (node.eta === undefined || node.k === undefined) return null
    return { dist: node.dist, slots: [{ constant: node.eta }, { constant: node.k }], names: ['eta'], constraints: ['positive'] }
  }
  const d = bayesDistribution(node.dist)
  if (!d) return null
  const slots: BayesPlanSlot[] = []
  for (const p of d.params) {
    const slot = node.args?.[p.name]
    if (!slot) return null
    slots.push(slot)
  }
  return { dist: node.dist, slots, names: d.params.map(p => p.name), constraints: d.params.map(p => p.constraint) }
}

/** The index of the first argument that breaks its constraint, or -1. */
function brokenArgument (dist: string, constraints: string[], args: number[]): number {
  for (let j = 0; j < constraints.length; j++) {
    const interval = bayesInterval(constraints[j])
    if (interval ? !bayesIntervalContains(interval, args[j]) : Number.isNaN(args[j])) return j
    if (!Number.isFinite(args[j])) return j
  }
  // a uniform needs its bounds in order
  if (dist === 'uniform' && !(args[0] < args[1])) return 1
  return -1
}

/** The nodes the target needs (itself included), or null when one of them is not in the plan. */
function closureOf (plan: BayesPriorPlan, target: string): { ids: string[], missing: boolean } {
  const wanted = new Set<string>()
  let missing = false
  const visit = (id: string): void => {
    if (wanted.has(id)) return
    wanted.add(id)
    const node = Object.hasOwn(plan.nodes, id) ? plan.nodes[id] : undefined
    if (!node) { missing = true; return }
    const reads = new Set<string>()
    for (const slot of Object.values(node.args ?? {})) slotReads(slot, reads)
    slotReads(node.value, reads)
    for (const r of reads) visit(r)
  }
  visit(target)
  return { ids: plan.order.filter(id => wanted.has(id)), missing }
}

function rangeOf (node: BayesPlanNode): { lower: number, upper: number } {
  let lower = node.range?.lower ?? Number.NEGATIVE_INFINITY
  let upper = node.range?.upper ?? Number.POSITIVE_INFINITY
  if (node.kind === 'correlation') { lower = Math.max(lower, -1); upper = Math.min(upper, 1) }
  return { lower, upper }
}

// ---------------------------------------------------------------- the closed form

/** True when every argument of the node's prior is a constant: the answer has no draw. */
function closedFormArgs (node: BayesPlanNode): { spec: PriorSpec, args: number[] } | null {
  if (node.kind !== 'parameter' && node.kind !== 'component' && node.kind !== 'state_init' && node.kind !== 'correlation') return null
  const spec = priorSpec(node)
  if (!spec) return null
  const args: number[] = []
  for (const slot of spec.slots) {
    const c = constantOf(slot)
    if (c === null) return null
    args.push(c)
  }
  return { spec, args }
}

/** Widens the axis to a finite bound of the range when it is near (the half-normal's wall at 0 is shown). */
function widenToBounds (lo: number, hi: number, lower: number, upper: number): [number, number] {
  const span = hi - lo
  if (Number.isFinite(lower) && lower <= lo && lo - lower <= BOUND_SHARE * span) lo = lower
  if (Number.isFinite(upper) && upper >= hi && upper - hi <= BOUND_SHARE * span) hi = upper
  return [lo, hi]
}

function closedForm (target: BayesPlanNode, spec: PriorSpec, args: number[], started: number): BayesPriorAnswer {
  const answer = (rest: Partial<BayesPriorAnswer>): BayesPriorAnswer => ({ target: target.id, kind: 'none', ms: now() - started, ...rest })
  const binding = bayesBinding(spec.dist)!
  if (brokenArgument(spec.dist, spec.constraints, args) >= 0) return answer({ none: 'errors' })
  const { lower, upper } = rangeOf(target)
  const truncated = Boolean(target.truncated)
  const atLow = truncated && lower > Number.NEGATIVE_INFINITY ? binding.cdf(lower, args) : 0
  const atHigh = truncated && upper < Number.POSITIVE_INFINITY ? binding.cdf(upper, args) : 1
  const mass = atHigh - atLow
  if (!(mass > 0)) return answer({ none: 'no_mass', truncated })
  // the rule 2 of the catalogue's bounds: the density over the mass, the quantile through the CDF's range
  const quantile = (p: number): number => truncated ? binding.quantile(atLow + p * mass, args) : binding.quantile(p, args)
  const q5 = quantile(0.05)
  const q50 = quantile(0.5)
  const q95 = quantile(0.95)
  let lo = quantile(CUT_BELOW)
  let hi = quantile(CUT_ABOVE)
  if (!(Number.isFinite(lo) && Number.isFinite(hi) && Number.isFinite(q5) && Number.isFinite(q95))) return answer({ none: 'errors' })
  if (!(hi > lo)) return answer({ none: 'constant', quantiles: { q5, q50, q95 }, truncated })
  ;[lo, hi] = widenToBounds(lo, hi, lower, upper)
  const x = new Float64Array(BAYES_PRIOR_CURVE_POINTS)
  const y = new Float64Array(BAYES_PRIOR_CURVE_POINTS)
  for (let k = 0; k < BAYES_PRIOR_CURVE_POINTS; k++) {
    // the last point is the end itself, not the end less a rounding
    x[k] = k === BAYES_PRIOR_CURVE_POINTS - 1 ? hi : lo + (hi - lo) * k / (BAYES_PRIOR_CURVE_POINTS - 1)
    const density = binding.pdf(x[k], args) / (truncated ? mass : 1)
    // a density that is infinite at a wall (a gamma of shape below 1 at 0) is a gap, not a spike
    y[k] = Number.isFinite(density) ? density : Number.NaN
  }
  const share = (v: number): number => truncated ? (binding.cdf(v, args) - atLow) / mass : binding.cdf(v, args)
  return answer({
    kind: 'curve',
    x, y,
    quantiles: { q5, q50, q95 },
    cutMass: { below: share(lo), above: 1 - share(hi) },
    truncated,
    mixture: false,
  })
}

// ---------------------------------------------------------------- ancestral draws

const now = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now())

interface Evaluation {
  drawn: Map<string, DrawnNode>
  causes: Map<string, Cause>
}

function count (causes: Map<string, Cause>, key: string, make: () => Cause, by: number): void {
  const known = causes.get(key)
  if (known) known.count += by
  else causes.set(key, { ...make(), count: by })
}

/**
 * Draws every node of the closure in order, each its whole array (the hyperparameters' draw i is
 * the argument of the draw i of what depends on them). A draw Stan would reject is NaN and is
 * counted once, at the node that rejects it.
 */
function drawNodes (plan: BayesPriorPlan, ids: string[], seed: number, pool: BayesPriorPool): Evaluation {
  const n = pool.size
  const stream = bayesStream(seed)
  const drawn = new Map<string, DrawnNode>()
  const causes = new Map<string, Cause>()
  const valuesOf = (id: string): Float64Array => {
    const d = drawn.get(id)
    if (!d) throw new Error(`prior preview: ${id} is read before it is drawn`)
    return d.values
  }
  ids.forEach((id, index) => {
    const node = plan.nodes[id]
    const values = pool.node(index)
    const readSet = new Set<string>()
    for (const slot of Object.values(node.args ?? {})) slotReads(slot, readSet)
    slotReads(node.value, readSet)
    const reads = [...readSet]
    const readArrays = reads.map(valuesOf)
    // a draw is inherited-rejected when something it reads is NaN there: not this node's cause
    const inputRejected = (i: number): boolean => {
      for (const r of readArrays) if (Number.isNaN(r[i])) return true
      return false
    }
    const taken: Float64Array[] = []
    const evalSlot = (slot: BayesPlanSlot): Values => {
      if ('constant' in slot) return slot.constant
      if ('ref' in slot) return valuesOf(slot.ref)
      const v = evalAst(slot.ast, valuesOf, pool, n)
      if (typeof v !== 'number' && isTemporary(v)) taken.push(v)
      return v
    }
    try {
      if (node.kind === 'transformed' || node.kind === 'state_scale') {
        const v = node.value ? evalSlot(node.value) : Number.NaN
        let own = 0
        for (let i = 0; i < n; i++) {
          const x = typeof v === 'number' ? v : v[i]
          if (Number.isFinite(x)) values[i] = x
          else {
            values[i] = Number.NaN
            if (!inputRejected(i)) own++
          }
        }
        if (own > 0) count(causes, `not_finite:${id}`, () => ({ reason: 'not_finite', node: id, count: 0 }), own)
      } else {
        drawPrior(node, id, values, evalSlot, inputRejected, stream.uniform, causes, n)
      }
    } finally {
      for (const t of taken) pool.give(t)
    }
    drawn.set(id, { node, values, reads })
  })
  return { drawn, causes }
}

function drawPrior (
  node: BayesPlanNode, id: string, out: Float64Array,
  evalSlot: (slot: BayesPlanSlot) => Values, inputRejected: (i: number) => boolean,
  uniform: () => number, causes: Map<string, Cause>, n: number,
): void {
  const spec = priorSpec(node)
  if (!spec) throw new Error(`prior preview: ${id} has no complete prior`)
  const binding = bayesBinding(spec.dist)!
  const slots = spec.slots.map(evalSlot)
  const { lower, upper } = rangeOf(node)
  const truncated = Boolean(node.truncated)
  const argument = (j: number): Cause => ({ reason: 'argument', node: id, argument: spec.names[j], constraint: spec.constraints[j], count: 0 })
  const args = new Array<number>(slots.length).fill(0)

  if (slots.every(s => typeof s === 'number')) {
    for (let j = 0; j < slots.length; j++) args[j] = slots[j] as number
    const broken = brokenArgument(spec.dist, spec.constraints, args)
    if (broken >= 0) {
      out.fill(Number.NaN)
      count(causes, `argument:${id}:${broken}`, () => argument(broken), n)
      return
    }
    const wrote = truncated ? binding.draw(uniform, args, out, lower, upper) : binding.draw(uniform, args, out)
    if (wrote === 0) {
      out.fill(Number.NaN)
      count(causes, `no_mass:${id}`, () => ({ reason: 'no_mass', node: id, count: 0 }), n)
      return
    }
    let bad = 0
    for (let i = 0; i < n; i++) if (!Number.isFinite(out[i])) { out[i] = Number.NaN; bad++ }
    if (bad > 0) count(causes, `not_finite:${id}`, () => ({ reason: 'not_finite', node: id, count: 0 }), bad)
    return
  }

  // an argument changes with the draw: each draw is made by inversion with its own arguments, from
  // one uniform (a truncation's two bounds are that draw's own, through its CDF)
  const tally = new Array<number>(slots.length).fill(0)
  let noMass = 0
  let notFinite = 0
  for (let i = 0; i < n; i++) {
    const u = Math.max(uniform(), Number.MIN_VALUE)
    for (let j = 0; j < slots.length; j++) {
      const s = slots[j]
      args[j] = typeof s === 'number' ? s : s[i]
    }
    if (inputRejected(i)) { out[i] = Number.NaN; continue }
    const broken = brokenArgument(spec.dist, spec.constraints, args)
    if (broken >= 0) { out[i] = Number.NaN; tally[broken]++; continue }
    let p = u
    if (truncated) {
      const atLow = lower > Number.NEGATIVE_INFINITY ? binding.cdf(lower, args) : 0
      const atHigh = upper < Number.POSITIVE_INFINITY ? binding.cdf(upper, args) : 1
      const mass = atHigh - atLow
      if (!(mass > 0)) { out[i] = Number.NaN; noMass++; continue }
      p = atLow + u * mass
    }
    const x = binding.quantile(Math.min(p, 1 - Number.EPSILON / 2), args)
    if (!Number.isFinite(x)) { out[i] = Number.NaN; notFinite++; continue }
    out[i] = truncated ? Math.min(Math.max(x, lower), upper) : x
  }
  for (let j = 0; j < tally.length; j++) if (tally[j] > 0) count(causes, `argument:${id}:${j}`, () => argument(j), tally[j])
  if (noMass > 0) count(causes, `no_mass:${id}`, () => ({ reason: 'no_mass', node: id, count: 0 }), noMass)
  if (notFinite > 0) count(causes, `not_finite:${id}`, () => ({ reason: 'not_finite', node: id, count: 0 }), notFinite)
}

function histogramAnswer (target: BayesPlanNode, ev: Evaluation, pool: BayesPriorPool, started: number): BayesPriorAnswer {
  const n = pool.size
  const values = ev.drawn.get(target.id)!.values
  const sorted = pool.scratch
  let used = 0
  for (let i = 0; i < n; i++) if (!Number.isNaN(values[i])) sorted[used++] = values[i]
  const leftOutCount = n - used
  let leftOut: BayesPriorAnswer['leftOut']
  if (leftOutCount > 0) {
    // the cause that rejected the most draws among everything the target needs
    let top: Cause | undefined
    for (const c of ev.causes.values()) if (!top || c.count > top.count) top = c
    if (top) leftOut = { count: leftOutCount, reason: top.reason, detail: { node: top.node, argument: top.argument, constraint: top.constraint } }
  }
  const base: Partial<BayesPriorAnswer> = {
    target: target.id, draws: n, used, mixture: true, truncated: Boolean(target.truncated),
  }
  if (leftOut) base.leftOut = leftOut
  const finish = (rest: Partial<BayesPriorAnswer>): BayesPriorAnswer => ({ kind: 'none', ...base, ...rest, ms: now() - started } as BayesPriorAnswer)
  if (used < 2) return finish({ none: 'no_mass' })
  const view = sorted.subarray(0, used)
  view.sort()
  const q = (p: number): number => quantileSorted(sorted, used, p)
  const quantiles = { q5: q(0.05), q50: q(0.5), q95: q(0.95) }
  let lo = q(CUT_BELOW)
  let hi = q(CUT_ABOVE)
  if (!(hi > lo)) return finish({ none: 'constant', quantiles })
  const { lower, upper } = rangeOf(target)
  ;[lo, hi] = widenToBounds(lo, hi, lower, upper)
  const edges = new Float64Array(BAYES_PRIOR_BINS + 1)
  for (let k = 0; k <= BAYES_PRIOR_BINS; k++) edges[k] = k === BAYES_PRIOR_BINS ? hi : lo + (hi - lo) * k / BAYES_PRIOR_BINS
  const counts = new Float64Array(BAYES_PRIOR_BINS)
  let below = 0
  let above = 0
  const width = (hi - lo) / BAYES_PRIOR_BINS
  for (let i = 0; i < used; i++) {
    const v = sorted[i]
    if (v < lo) below++
    else if (v > hi) above++
    else counts[Math.min(BAYES_PRIOR_BINS - 1, Math.floor((v - lo) / width))]++
  }
  return finish({ kind: 'histogram', x: edges, y: counts, quantiles, cut: { below, above } })
}

// ---------------------------------------------------------------- the entry points

/**
 * What one quantity's prior implies: the closed-form curve when its arguments are constants, else
 * the histogram of 4,000 ancestral draws from the seeded stream. Nodes that need data, or that
 * have error findings above them, are not drawn and the answer says why.
 */
export function bayesPriorPreview (plan: BayesPriorPlan, target: string, seed: number, draws: number = BAYES_PRIOR_DRAWS): BayesPriorAnswer {
  const started = now()
  const none = (why: NonNullable<BayesPriorAnswer['none']>): BayesPriorAnswer => ({ target, kind: 'none', none: why, ms: now() - started })
  const node = Object.hasOwn(plan.nodes, target) ? plan.nodes[target] : undefined
  if (!node) return none('needs_data')
  const { ids, missing } = closureOf(plan, target)
  if (missing) return none('needs_data')
  if (ids.some(id => plan.nodes[id].errors > 0)) return none('errors')
  try {
    const exact = closedFormArgs(node)
    if (exact) return closedForm(node, exact.spec, exact.args, started)
    if ((node.kind === 'parameter' || node.kind === 'component' || node.kind === 'state_init') && !priorSpec(node)) return none('errors')
    const pool = poolFor(draws)
    const ev = drawNodes(plan, ids, seed, pool)
    return histogramAnswer(node, ev, pool, started)
  } catch (error) {
    // a binding or an evaluation that throws is an answer of «none», never an exception in the worker
    console.error('prior preview:', error)
    return none('errors')
  }
}

/**
 * Every quantity of the plan that has no data and no error above it, drawn `n` times from the
 * seed, as copies (a `NaN` is a draw Stan would reject): the browser's half of the golden test
 * against Stan, the same code the worker runs. A closed-form quantity is drawn too, by the
 * binding, so the comparison is of draws with draws.
 */
export function bayesDrawMarginals (plan: BayesPriorPlan, seed: number, n: number = BAYES_PRIOR_DRAWS): Record<string, Float64Array> {
  const pool = poolFor(n)
  const ids = plan.order.filter(id => plan.nodes[id].errors === 0)
  const ev = drawNodes(plan, ids, seed, pool)
  const out: Record<string, Float64Array> = {}
  for (const [id, d] of ev.drawn) out[id] = d.values.slice()
  return out
}
