// What the inspector of a Bayesian model reads from the mirror (docs/specs/bayes-graph-editor.md,
// «The inspector», «Expressions while typing», lot G2): for each slot of the selected node its first
// error and where to underline it, the type the mirror gave it, the hyperprior it reads; the names and
// functions offered at the caret of an expression; the order of the Problems tab.
//
// Pure functions, nothing of Vue, Nuxt or the DOM: the tests import them as they are. Every export
// starts with `bayes`, `BAYES_` or `Bayes`: a composable's exports are auto-imported everywhere.
import { BAYES_CATALOG, type BayesCatalog } from '~/composables/bayesCatalog'
import { bayesSlotKey, type BayesGraphFinding, type BayesModel, type BayesTypedSlot } from '~/composables/bayesGraph'
import type { BayesDoc, BayesSlotPlace } from '~/composables/bayesGraphEdits'

const owner = (id: string): string => { const i = id.indexOf('.'); return i < 0 ? id : id.slice(0, i) }

/**
 * Whether a slot is read before any data (a prior's argument, a state's first value and scale, an
 * effect's mean and scale, the correlation's η): no data node may be named there (`graph.prior_data`),
 * so the names' list leaves them out.
 */
export function bayesIsPriorPath (path: string): boolean {
  return /^(prior|init)\./.test(path) || path === 'scale' || /^\w+\.(mean|scale)$/.test(path) || path === 'correlation.eta'
}

/** A slot of the selected node as the inspector shows it. */
export interface BayesSlotView {
  place: BayesSlotPlace
  /** The first error the mirror finds on it, and the span to underline (in code points). */
  error: BayesGraphFinding | null
  pos: number
  len: number
  /** The first warning on it, when it has no error. */
  warning: BayesGraphFinding | null
  /** What the mirror typed, when it typed. */
  typed: BayesTypedSlot | null
  /** The nodes it reads (owners, once each, in the order read). */
  refs: string[]
  /** The one parameter it reads when that parameter is a hyperprior (its id), or ''. */
  hyper: string
}

/** A slot's errors, warnings and type, from the mirror's findings and its model. */
export function bayesSlotView (place: BayesSlotPlace, findings: readonly BayesGraphFinding[], model: BayesModel | null): BayesSlotView {
  const mine = findings.filter(f => f.node === place.node && f.slot === place.slot)
  const error = mine.find(f => f.severity === 'error') ?? null
  const warning = error ? null : mine.find(f => f.severity === 'warning') ?? null
  const a = (error?.args ?? {}) as Record<string, unknown>
  const whole = Array.from(place.text).length
  const pos = typeof a.pos === 'number' ? a.pos : 0
  const len = typeof a.pos === 'number' ? (typeof a.len === 'number' ? a.len : 0) : whole
  const typed = model?.slots.get(bayesSlotKey(place.node, place.slot)) ?? null
  const refs: string[] = []
  for (const r of typed?.refs ?? []) if (!refs.includes(owner(r))) refs.push(owner(r))
  const params = refs.filter(id => model?.graph.nodes.some(n => n.id === id && n.kind === 'parameter'))
  const hyper = !error && params.length === 1 && refs.length === 1 && model?.params.get(params[0]!)?.role === 'hyperprior' ? params[0]! : ''
  return { place, error, pos, len, warning, typed, refs, hyper }
}

/** How a value's plate is said: a single number, one per row, one per unit of a box, one per level. */
export type BayesPlateWords =
  | { key: 'single' }
  | { key: 'perRow' }
  | { key: 'perUnit', plate: string }
  | { key: 'perLevel', node: string }

/** The words of a plate id ('' a single value, the rows, a group box, `levels:<category>`). */
export function bayesPlateWords (plate: string, doc: Pick<BayesDoc, 'plates'>): BayesPlateWords {
  if (!plate) return { key: 'single' }
  if (plate.startsWith('levels:')) return { key: 'perLevel', node: plate.slice('levels:'.length) }
  if (doc.plates.find(p => p.id === plate)?.kind === 'rows') return { key: 'perRow' }
  return { key: 'perUnit', plate }
}

// ---------------------------------------------------------------- the names at the caret

/** A name or a function offered in an expression. */
export interface BayesNameChoice {
  /** What is shown and matched: the name, or the function's name. */
  text: string
  /** What is written in its place: the name, or the function's name and its parenthesis. */
  insert: string
  kind: 'node' | 'function'
  /** For a node: its id (a component's `<node>.<component>`), its kind and its plate ('' none). */
  node?: string
  role?: string
  plate?: string
}

/**
 * The names an expression can read (the graph's names, the components' names) and the language's
 * functions. In a slot read before any data, no data node and no `lag`; a time node is never a
 * number, nor is a correlation, and a likelihood has no name: none of them is offered.
 */
export function bayesNameChoices (doc: BayesDoc, prior: boolean, catalog: BayesCatalog = BAYES_CATALOG): BayesNameChoice[] {
  const out: BayesNameChoice[] = []
  for (const n of doc.nodes) {
    const plate = typeof n.plate === 'string' ? n.plate : ''
    if (n.kind === 'correlated_effects') {
      for (const c of n.components ?? []) if (c.name) out.push({ text: c.name, insert: c.name, kind: 'node', node: `${n.id}.${c.id}`, role: 'component', plate })
      continue
    }
    if (n.kind === 'likelihood' || typeof n.name !== 'string' || !n.name) continue
    if (n.kind === 'data' && (prior || n.type === 'time')) continue
    out.push({ text: n.name, insert: n.name, kind: 'node', node: n.id, role: n.kind === 'data' ? `data_${String(n.type ?? 'real')}` : n.kind, plate })
  }
  out.sort((a, b) => (a.text < b.text ? -1 : a.text > b.text ? 1 : 0))
  for (const f of catalog.language.functions) {
    if (prior && f.name === 'lag') continue
    out.push({ text: f.name, insert: `${f.name}(`, kind: 'function' })
  }
  return out
}

const WORD = /[A-Za-z0-9_]/

/** The word the caret is in or after: its start, its end, and the part before the caret. */
export function bayesWordAt (text: string, caret: number): { start: number, end: number, word: string } {
  let start = Math.max(0, Math.min(caret, text.length))
  let end = start
  while (start > 0 && WORD.test(text[start - 1]!)) start--
  while (end < text.length && WORD.test(text[end]!)) end++
  const word = text.slice(start, caret)
  // a number is not a name: nothing to complete in `2.5` or `1e3`
  if (/^[0-9]/.test(word)) return { start: caret, end: caret, word: '' }
  return { start, end, word }
}

/** The choices that fit a word: those that start with it first, then those that hold it; all for ''. */
export function bayesFilterNames (choices: readonly BayesNameChoice[], word: string, max = 8): BayesNameChoice[] {
  const w = word.toLowerCase()
  if (!w) return choices.slice(0, max)
  const starts = choices.filter(c => c.text.toLowerCase().startsWith(w))
  const holds = choices.filter(c => !c.text.toLowerCase().startsWith(w) && c.text.toLowerCase().includes(w))
  // a word already written whole offers nothing more
  const all = [...starts, ...holds]
  if (all.length === 1 && all[0]!.text === word) return []
  return all.slice(0, max)
}

/** The text with the word at the caret replaced by the choice, and the caret after it. */
export function bayesInsertName (text: string, caret: number, choice: BayesNameChoice): { text: string, caret: number } {
  const { start, end } = bayesWordAt(text, caret)
  let insert = choice.insert
  // a function already followed by its parenthesis keeps the one there
  if (choice.kind === 'function' && text[end] === '(') insert = choice.text
  const out = text.slice(0, start) + insert + text.slice(end)
  return { text: out, caret: start + insert.length + (insert === choice.text && choice.kind === 'function' ? 1 : 0) }
}

// ---------------------------------------------------------------- the Problems tab

/** A finding of the Problems tab, with the node or box it belongs to ('' the whole model). */
export interface BayesProblem { finding: BayesGraphFinding, owner: string }

/**
 * Every finding in the order the Problems tab lists them: errors before warnings; the model's own
 * first, then by the order of the nodes and boxes in the document (the order they were drawn), then
 * as the mirror sorted them.
 */
export function bayesProblems (findings: readonly BayesGraphFinding[], doc: Pick<BayesDoc, 'nodes' | 'plates'>): BayesProblem[] {
  const order = new Map<string, number>()
  doc.plates.forEach((p, i) => order.set(p.id, i))
  doc.nodes.forEach((n, i) => order.set(n.id, doc.plates.length + i))
  const rank = (id: string): number => (id === '' ? -1 : order.get(id) ?? Number.MAX_SAFE_INTEGER)
  return findings
    .map((finding, i) => ({ finding, owner: owner(finding.node), i }))
    .sort((a, b) => (a.finding.severity === b.finding.severity ? 0 : a.finding.severity === 'error' ? -1 : 1) || rank(a.owner) - rank(b.owner) || a.i - b.i)
    .map(({ finding, owner }) => ({ finding, owner }))
}
