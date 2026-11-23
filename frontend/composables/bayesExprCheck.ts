// The expression language's type checker and static intervals as the browser reads them: a mirror
// of the gateway's (gateway-go/internal/bayesexpr/check.go and interval.go,
// docs/specs/bayes-expression-language.md, «Types and shapes», «The rules», «Intervals»). The
// gateway is the one authority and never believes this copy: it gives the person feedback while
// typing, and the tests make it give the same answer as the Go checker on every typed case of the
// shared corpus: the same error code, position, length and arguments, the same type, and the same
// interval, every end the same double (the arithmetic is IEEE, every step in the order Go does it).
//
// Pure functions, nothing of Nuxt or the DOM. Every export starts with `bayes`, `BAYES_` or
// `Bayes`: a composable's exports are auto-imported everywhere.

import { BAYES_CATALOG, bayesLimit, type BayesCatalog, type BayesInterval } from '~/composables/bayesCatalog'
import { bayesParseExpr, type BayesExprCode, type BayesExprError, type BayesExprNode, type BayesExprSpan } from '~/composables/bayesExpr'

/** What the values of a node are: numbers, whole numbers, a group index, a series' order, or not a number. */
export type BayesElem = 'real' | 'int' | 'index' | 'time' | 'not_number'

/** The type of a node or a value: a plate id ('' = a single value); an index also names its target plate. */
export interface BayesExprType {
  elem: BayesElem
  plate: string
  target?: string
}

export type BayesRole =
  | 'data' | 'parameter' | 'transformed' | 'linear_predictor' | 'state' | 'component' | 'correlation'

/** A set of reals: lo may be -Infinity, hi Infinity (an infinite end is open). */
export interface BayesExprInterval {
  lo: number
  hi: number
  loOpen: boolean
  hiOpen: boolean
}

/** A named node of the graph, as the environment gives it. */
export interface BayesExprSymbol {
  id: string
  role: BayesRole
  type: BayesExprType
  /** The declared interval (a parameter's range, a data node's support); absent is unbounded. */
  interval?: BayesExprInterval | null
}

/**
 * What the model graph gives for one slot. `encloses` is reflexive; a group plate encloses the
 * plates nested in it and the rows; it is never called with the scalar plate '' (nor `isSeries`).
 */
export interface BayesExprEnv {
  lookup: (name: string) => BayesExprSymbol | undefined
  context: () => string
  encloses: (outer: string, inner: string) => boolean
  isSeries: (plate: string) => boolean
  /** The name of the group index whose target is `target`, for messages. */
  indexOf: (target: string) => string | undefined
}

/** A node of a checked tree with its type. Only a leaf is ever int. */
export interface BayesExprTyped {
  node: BayesExprNode
  type: BayesExprType
  args: BayesExprTyped[]
  sym?: BayesExprSymbol
  by?: BayesExprSymbol
  lifted: boolean
  convert: boolean
}

export interface BayesExprLift {
  index: string
  id: string
  rows: string
  context: string
}

export type BayesCheckResult =
  | {
    ok: true
    tree: BayesExprTyped
    context: string
    /** The distinct k of its lags, ascending. */
    lags: number[]
    lifts: BayesExprLift[]
    type: BayesExprType
    interval: BayesExprInterval
  }
  | { ok: false, error: BayesExprError }

// ---------------------------------------------------------------- intervals

export const bayesUnbounded = (): BayesExprInterval => ({ lo: -Infinity, hi: Infinity, loOpen: true, hiOpen: true })

/** The step after every operation: NaN or an overflow gives the real line; an infinite end is open; -0 is 0. */
function tidy (i: BayesExprInterval): BayesExprInterval {
  if (Number.isNaN(i.lo) || Number.isNaN(i.hi) || i.lo === Infinity || i.hi === -Infinity) return bayesUnbounded()
  const r = { ...i }
  if (r.lo === -Infinity) r.loOpen = true
  if (r.hi === Infinity) r.hiOpen = true
  if (r.lo === 0) r.lo = 0
  if (r.hi === 0) r.hi = 0
  return r
}

/** [v, v]. */
export const bayesIntervalPoint = (v: number): BayesExprInterval => tidy({ lo: v, hi: v, loOpen: false, hiOpen: false })

/** A catalogue interval (a missing end is infinite). */
export function bayesIntervalFromCatalog (c: BayesInterval): BayesExprInterval {
  const i = bayesUnbounded()
  if (c.lower !== undefined) {
    i.lo = c.lower
    i.loOpen = Boolean(c.lower_open)
  }
  if (c.upper !== undefined) {
    i.hi = c.upper
    i.hiOpen = Boolean(c.upper_open)
  }
  return tidy(i)
}

/** The interval in the catalogue's form (an infinite end is left out). */
export function bayesIntervalToCatalog (i: BayesExprInterval): BayesInterval {
  const c: BayesInterval = {}
  if (Number.isFinite(i.lo)) {
    c.lower = i.lo
    if (i.loOpen) c.lower_open = true
  }
  if (Number.isFinite(i.hi)) {
    c.upper = i.hi
    if (i.hiOpen) c.upper_open = true
  }
  return c
}

const neg = (a: BayesExprInterval): BayesExprInterval => tidy({ lo: -a.hi, hi: -a.lo, loOpen: a.hiOpen, hiOpen: a.loOpen })

const add = (a: BayesExprInterval, b: BayesExprInterval): BayesExprInterval =>
  tidy({ lo: a.lo + b.lo, hi: a.hi + b.hi, loOpen: a.loOpen || b.loOpen, hiOpen: a.hiOpen || b.hiOpen })

const sub = (a: BayesExprInterval, b: BayesExprInterval): BayesExprInterval => add(a, neg(b))

interface End { v: number, closed: boolean }

function corner (x: End, y: End): End {
  if ((x.v === 0 && x.closed) || (y.v === 0 && y.closed)) return { v: 0, closed: true }
  if (x.v === 0 || y.v === 0) return { v: 0, closed: false }
  return { v: x.v * y.v, closed: x.closed && y.closed }
}

function mul (a: BayesExprInterval, b: BayesExprInterval): BayesExprInterval {
  const alo = { v: a.lo, closed: !a.loOpen }
  const ahi = { v: a.hi, closed: !a.hiOpen }
  const blo = { v: b.lo, closed: !b.loOpen }
  const bhi = { v: b.hi, closed: !b.hiOpen }
  const cs = [corner(alo, blo), corner(alo, bhi), corner(ahi, blo), corner(ahi, bhi)]
  let lo = cs[0].v
  let hi = cs[0].v
  for (const c of cs.slice(1)) {
    if (c.v < lo) lo = c.v
    if (c.v > hi) hi = c.v
  }
  let loClosed = false
  let hiClosed = false
  for (const c of cs) {
    if (c.v === lo && c.closed) loClosed = true
    if (c.v === hi && c.closed) hiClosed = true
  }
  return tidy({ lo, hi, loOpen: !loClosed, hiOpen: !hiClosed })
}

const containsZero = (i: BayesExprInterval): boolean =>
  (i.lo < 0 || (i.lo === 0 && !i.loOpen)) && (i.hi > 0 || (i.hi === 0 && !i.hiOpen))

function div (a: BayesExprInterval, b: BayesExprInterval): BayesExprInterval {
  if (containsZero(b)) return bayesUnbounded()
  const r: BayesExprInterval = {
    lo: b.hi === 0 ? -Infinity : 1 / b.hi,
    hi: b.lo === 0 ? Infinity : 1 / b.lo,
    loOpen: b.hiOpen,
    hiOpen: b.loOpen,
  }
  return mul(a, tidy(r))
}

export type BayesIntervalRelation = 'inside' | 'overlap' | 'disjoint'

/** How `i` stands to the interval a slot needs: every value allowed, some, none. */
export function bayesIntervalWithin (i: BayesExprInterval, need: BayesExprInterval): BayesIntervalRelation {
  const loIn = i.lo > need.lo || (i.lo === need.lo && (i.loOpen || !need.loOpen))
  const hiIn = i.hi < need.hi || (i.hi === need.hi && (i.hiOpen || !need.hiOpen))
  if (loIn && hiIn) return 'inside'
  const below = i.hi < need.lo || (i.hi === need.lo && (i.hiOpen || need.loOpen))
  const above = i.lo > need.hi || (i.lo === need.hi && (i.loOpen || need.hiOpen))
  if (below || above) return 'disjoint'
  return 'overlap'
}

const POSITIVE_RAY: BayesExprInterval = { lo: 0, hi: Infinity, loOpen: true, hiOpen: true }

function pow (a: BayesExprInterval): BayesExprInterval {
  return bayesIntervalWithin(a, POSITIVE_RAY) === 'inside' ? { ...POSITIVE_RAY } : bayesUnbounded()
}

/** The catalogue's named interval (real, positive, unit…); an unknown id is unbounded. */
export function bayesNamedInterval (id: string, catalog: BayesCatalog = BAYES_CATALOG): BayesExprInterval {
  const c = catalog.intervals[id]
  return c ? bayesIntervalFromCatalog(c) : bayesUnbounded()
}

/** The static interval of a checked value. */
export function bayesTypedInterval (t: BayesExprTyped, catalog: BayesCatalog = BAYES_CATALOG): BayesExprInterval {
  const n = t.node
  switch (n.kind) {
    case 'num': return bayesIntervalPoint(n.num)
    case 'name': case 'index': case 'lag':
      return t.sym?.interval ? tidy(t.sym.interval) : bayesUnbounded()
    case 'call': {
      const f = catalog.language.functions.find(g => g.name === n.name && !g.apart)
      return bayesNamedInterval(f?.range ?? '', catalog)
    }
    case 'neg': return neg(bayesTypedInterval(t.args[0], catalog))
    case 'binary': {
      const a = bayesTypedInterval(t.args[0], catalog)
      const b = bayesTypedInterval(t.args[1], catalog)
      switch (n.op) {
        case '+': return add(a, b)
        case '-': return sub(a, b)
        case '*': return mul(a, b)
        case '/': return div(a, b)
      }
      return pow(a)
    }
  }
}

// ---------------------------------------------------------------- types

/** The type as the corpus writes it: real, int, real[rows], int[p1], index[rows->p1], time[rows]. */
export function bayesExprTypeText (t: BayesExprType): string {
  if (t.elem === 'index') return `index[${t.plate}->${t.target ?? ''}]`
  return t.plate === '' ? t.elem : `${t.elem}[${t.plate}]`
}

class CheckFail {
  error: BayesExprError
  constructor (error: BayesExprError) {
    this.error = error
  }
}

/** The error's args, leaving out the empty values (the scalar plate). */
function argsOf (...kv: string[]): Record<string, string> {
  const m: Record<string, string> = {}
  for (let i = 0; i + 1 < kv.length; i += 2) if (kv[i + 1] !== '') m[kv[i]] = kv[i + 1]
  return m
}

const fail = (s: BayesExprSpan, code: BayesExprCode, args: Record<string, string>): never => {
  throw new CheckFail({ code, pos: s.pos, len: s.len, args })
}

const own = (n: BayesExprNode): BayesExprSpan => n.at ?? { pos: n.pos, len: n.len }

/** The roles lag may take. */
const LAG_ROLES: BayesRole[] = ['data', 'parameter', 'transformed', 'state']

class Checker {
  lags: number[] = []
  lifts: BayesExprLift[] = []
  ctx: string
  maxLag: number

  private env: BayesExprEnv
  private catalog: BayesCatalog

  constructor (env: BayesExprEnv, catalog: BayesCatalog) {
    this.env = env
    this.catalog = catalog
    this.ctx = env.context()
    this.maxLag = bayesLimit('max_lag', catalog)
  }

  /** The type of a value over the plates of its operands: the context when one is not a single value. */
  over (elem: BayesElem, ...args: BayesExprTyped[]): BayesExprType {
    return { elem, plate: args.some(a => a.type.plate !== '') ? this.ctx : '' }
  }

  /** The integer leaves among the operands are converted to real. */
  operands (...args: BayesExprTyped[]): BayesExprTyped[] {
    for (const a of args) if (a.type.elem === 'int') a.convert = true
    return args
  }

  typed (node: BayesExprNode, type: BayesExprType, extra: Partial<BayesExprTyped> = {}): BayesExprTyped {
    return { node, type, args: [], lifted: false, convert: false, ...extra }
  }

  check (n: BayesExprNode): BayesExprTyped {
    switch (n.kind) {
      case 'num': return this.typed(n, { elem: 'real', plate: '' })
      case 'name': return this.name(n)
      case 'index': return this.index(n)
      case 'call': return this.call(n)
      case 'lag': return this.lag(n)
      case 'neg': {
        const x = this.check(n.args[0])
        return this.typed(n, this.over('real', x), { args: this.operands(x) })
      }
      case 'binary': {
        const l = this.check(n.args[0])
        const r = this.check(n.args[1])
        return this.typed(n, this.over('real', l, r), { args: this.operands(l, r) })
      }
    }
  }

  /** A name that must be a number: unknown nodes, time nodes and values that are not numbers are refused at s. */
  lookup (name: string, s: BayesExprSpan): BayesExprSymbol {
    const sym = this.env.lookup(name)
    if (!sym) return fail(s, 'expr.unknown_name', argsOf('name', name))
    if (sym.type.elem === 'time') return fail(s, 'expr.time_value', argsOf('name', name))
    if (sym.type.elem === 'not_number') return fail(s, 'expr.not_number', argsOf('name', name))
    return sym
  }

  name (n: Extract<BayesExprNode, { kind: 'name' }>): BayesExprTyped {
    const s = own(n)
    const sym = this.lookup(n.name, s)
    if (sym.type.elem === 'index') {
      return fail(s, 'expr.index_alone', argsOf('name', n.name, 'target', sym.type.target ?? ''))
    }
    const p = sym.type.plate
    if (p === '') return this.typed(n, { elem: sym.type.elem, plate: '' }, { sym })
    if (p === this.ctx) return this.typed(n, { elem: sym.type.elem, plate: this.ctx }, { sym })
    if (this.ctx !== '' && this.env.encloses(p, this.ctx)) {
      return fail(s, 'expr.needs_index', argsOf('name', n.name, 'plate', p, 'index', this.env.indexOf(p) ?? ''))
    }
    return fail(s, 'expr.wrong_plate', argsOf('name', n.name, 'plate', p, 'context', this.ctx))
  }

  index (n: Extract<BayesExprNode, { kind: 'index' }>): BayesExprTyped {
    const [xs, gs] = n.sub as [BayesExprSpan, BayesExprSpan]
    const x = this.lookup(n.name, xs)
    if (x.type.elem === 'index') {
      return fail(xs, 'expr.index_alone', argsOf('name', n.name, 'target', x.type.target ?? ''))
    }
    if (x.type.plate === '') return fail(xs, 'expr.index_scalar', argsOf('name', n.name, 'by', n.by))
    const g = this.lookup(n.by, gs)
    if (g.type.elem !== 'index') return fail(gs, 'expr.not_index', argsOf('name', n.by))
    const rows = g.type.plate
    const target = g.type.target ?? ''
    if (x.type.plate !== target) {
      return fail(own(n), 'expr.index_target', argsOf('name', n.name, 'plate', x.type.plate, 'by', n.by, 'target', target))
    }
    let lifted = false
    if (this.ctx === rows) {
      // on the context
    } else if (this.ctx !== '' && this.env.encloses(target, this.ctx) && this.env.encloses(this.ctx, rows)) {
      lifted = true
      this.lifts.push({ index: n.by, id: g.id, rows, context: this.ctx })
    } else {
      return fail(own(n), 'expr.index_context',
        argsOf('name', n.name, 'by', n.by, 'rows', rows, 'target', target, 'context', this.ctx))
    }
    return this.typed(n, { elem: x.type.elem, plate: this.ctx }, { sym: x, by: g, lifted })
  }

  call (n: Extract<BayesExprNode, { kind: 'call' }>): BayesExprTyped {
    const s = own(n)
    const nameSpan = { pos: s.pos, len: n.name.length }
    const f = this.catalog.language.functions.find(g => g.name === n.name && !g.apart)
    if (!f) {
      if (this.env.lookup(n.name)) return fail(nameSpan, 'expr.not_function', argsOf('name', n.name))
      return fail(nameSpan, 'expr.unknown_function', argsOf('name', n.name))
    }
    if (n.args.length !== f.arguments) {
      return fail(s, 'expr.arity', argsOf('name', n.name, 'want', String(f.arguments), 'got', String(n.args.length)))
    }
    const args = n.args.map(a => this.check(a))
    return this.typed(n, this.over('real', ...args), { args: this.operands(...args) })
  }

  lag (n: Extract<BayesExprNode, { kind: 'lag' }>): BayesExprTyped {
    const [xs, ks] = n.sub as [BayesExprSpan, BayesExprSpan]
    if (this.ctx === '' || !this.env.isSeries(this.ctx)) {
      return fail(own(n), 'expr.lag_not_series', argsOf('name', n.name, 'context', this.ctx))
    }
    const x = this.lookup(n.name, xs)
    let reason = ''
    if (x.type.elem === 'index') reason = 'index'
    else if (!LAG_ROLES.includes(x.role)) reason = 'kind'
    else if (x.type.plate !== this.ctx) reason = 'plate'
    if (reason !== '') return fail(xs, 'expr.lag_argument', argsOf('name', n.name, 'reason', reason))
    if (n.k < 1 || n.k > this.maxLag) return fail(ks, 'expr.lag_k', argsOf('max', String(this.maxLag)))
    this.lags.push(n.k)
    // A lag is real over the context; an integer node is converted.
    return this.typed(n, { elem: 'real', plate: this.ctx }, { sym: x, convert: x.type.elem === 'int' })
  }
}

/**
 * Types a tree against `env`: the typed tree, its type and interval, or the first error found
 * walking the tree left to right (a node's own errors before its operands').
 */
export function bayesCheckExpr (node: BayesExprNode, env: BayesExprEnv, catalog: BayesCatalog = BAYES_CATALOG): BayesCheckResult {
  const c = new Checker(env, catalog)
  try {
    const tree = c.check(node)
    const lifts: BayesExprLift[] = []
    for (const l of [...c.lifts].sort((a, b) => (a.index < b.index ? -1 : a.index > b.index ? 1 : 0))) {
      const last = lifts[lifts.length - 1]
      if (!last || last.index !== l.index || last.id !== l.id || last.rows !== l.rows || last.context !== l.context) lifts.push(l)
    }
    const lags = [...new Set(c.lags)].sort((a, b) => a - b)
    return { ok: true, tree, context: c.ctx, lags, lifts, type: tree.type, interval: bayesTypedInterval(tree, catalog) }
  } catch (e) {
    if (e instanceof CheckFail) return { ok: false, error: e.error }
    throw e
  }
}

/** Reads `src` and types it against `env`: the first error, of syntax or of types. */
export function bayesCheckExprText (src: string, env: BayesExprEnv, catalog: BayesCatalog = BAYES_CATALOG): BayesCheckResult {
  const parsed = bayesParseExpr(src, catalog)
  if (!parsed.ok) return { ok: false, error: parsed.error }
  return bayesCheckExpr(parsed.node, env, catalog)
}
