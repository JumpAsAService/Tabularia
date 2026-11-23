// What the editor of Bayesian models writes about a graph (docs/specs/bayes-graph-editor.md): the key
// of each finding's message and its placeholders with the plates' labels in place of their ids (the
// mirror's findings name plates and nodes by id: «the UI substitutes labels before formatting messages»), a
// prior written as text (`normal(0, 2.5)`), a parameter's bounds, a constraint's words.
//
// Pure functions: the words are in i18n/locales/*.gen.ts, under the keys these return. Every export
// starts with `bayes` or `Bayes`: a composable's exports are auto-imported everywhere.
import { bayesGraphMessageKey, type BayesGraphFinding } from '~/composables/bayesGraph'

/** A message to format: its key and its placeholders. */
export interface BayesText { key: string, params: Record<string, string> }

/** The expression codes whose message depends on an argument, and how it is chosen. */
function exprVariant (code: string, args: Record<string, unknown>): string {
  const has = (k: string): boolean => args[k] !== undefined && args[k] !== ''
  switch (code) {
    case 'non_ascii':
      if (has('direction')) return 'non_ascii.direction'
      if (has('char')) return args.ascii ? 'non_ascii.look_alike' : 'non_ascii.invisible'
      return 'non_ascii.plain'
    case 'number_form': return `number_form.${String(args.kind)}`
    case 'number_range': return `number_range.${String(args.reason)}`
    case 'unexpected': return `unexpected.${String(args.expected)}`
    case 'needs_index': return has('index') ? 'needs_index.named' : 'needs_index.plain'
    case 'wrong_plate': case 'index_context': case 'lag_not_series':
      return `${code}.${has('context') ? 'context' : 'plain'}`
    case 'lag_argument': return has('reason') ? `lag_reason.${String(args.reason)}` : 'lag_argument'
    default: return code
  }
}

/**
 * The placeholders that hold a plate's or a node's id, written with its label: the plates of the
 * expression's messages, and the data, the node a prior reaches them through, the observed data, the
 * lagged value of the graph's (`prior_data`, `self_predictor`, `lag_nested`, `terms`, `likelihood`).
 */
const ID_ARGS = new Set(['plate', 'context', 'rows', 'target', 'data', 'through', 'observed', 'value', 'expected'])

function params (args: Record<string, unknown> | undefined, label: (id: string) => string | undefined): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(args ?? {})) {
    if (Array.isArray(v)) out[k] = v.map(String).join(', ')
    else if (ID_ARGS.has(k) && typeof v === 'string') out[k] = label(v) ?? v
    else out[k] = String(v)
  }
  return out
}

/**
 * The message of a finding of the mirror (or of the gateway): `bayes.graph.<…>` for the graph's
 * codes, `bayes.expr.<…>` for an expression's (a hint's message is the catalogue's own key). Plate
 * and node ids in the placeholders become their labels (`label` returns undefined for an id it does
 * not know, which then stays as it is).
 */
export function bayesFindingText (f: Pick<BayesGraphFinding, 'code' | 'args' | 'slot' | 'node' | 'severity'>, label: (id: string) => string | undefined = () => undefined): BayesText {
  const args = (f.args ?? {}) as Record<string, unknown>
  if (f.code === 'expr.hint' && typeof args.hint === 'string') return { key: args.hint, params: params(args, label) }
  if (f.code.startsWith('expr.')) return { key: `bayes.expr.${exprVariant(f.code.slice(5), args)}`, params: params(args, label) }
  return { key: `bayes.graph.${bayesGraphMessageKey(f as BayesGraphFinding)}`, params: params(args, label) }
}

/** An expression's span in UTF-16 code units: the mirror counts code points. */
export function bayesSpanUnits (text: string, pos: number, len: number): { start: number, end: number } {
  const points = Array.from(text)
  const start = points.slice(0, pos).join('').length
  const end = start + points.slice(pos, pos + Math.max(len, 0)).join('').length
  return { start, end: Math.max(end, start) }
}

/** The argument names of the catalogue as the textbooks write them. */
const SYMBOLS: Record<string, string> = {
  mu: 'μ', sigma: 'σ', beta: 'β', alpha: 'α', nu: 'ν', eta: 'η', phi: 'φ', lambda: 'λ', theta: 'θ', tau: 'τ', rho: 'ρ',
}

/** An argument's symbol (`μ` for `mu`), or its name when it has none. */
export function bayesArgSymbol (name: string): string {
  return SYMBOLS[name] ?? name
}

/** A prior written as text, its arguments in the distribution's order: `normal(0, 2.5)`. */
export function bayesPriorText (dist: string, args: Record<string, { expr?: string | null, ref?: string | null } | undefined>, order: readonly string[]): string {
  const names = order.length ? order : Object.keys(args)
  const parts = names.map(n => {
    const s = args[n]
    return (s?.expr ?? s?.ref ?? '').trim() || '…'
  })
  return `${dist}(${parts.join(', ')})`
}

/** A parameter's bounds as a short mark: `≥ 0`, `≤ 1`, `0 … 1`; '' with none. */
export function bayesBoundsText (bounds: { lower?: number | null, upper?: number | null } | null | undefined, num: (v: number) => string = String): string {
  const lo = bounds?.lower
  const hi = bounds?.upper
  if (lo != null && hi != null) return `${num(lo)} … ${num(hi)}`
  if (lo != null) return `≥ ${num(lo)}`
  if (hi != null) return `≤ ${num(hi)}`
  return ''
}

/** The constraints a prior's argument can have that the inspector says in words. */
export const BAYES_SAID_CONSTRAINTS = ['positive', 'non_negative_integer'] as const

/** The words of an argument's constraint (`bayes.editor.constraint.<c>`), null when there is nothing to say (any real). */
export function bayesConstraintKey (constraint: string): string | null {
  return (BAYES_SAID_CONSTRAINTS as readonly string[]).includes(constraint) ? `bayes.editor.constraint.${constraint}` : null
}
