// The editor's assistant of the Bayesian models, without Vue (docs/specs/bayes-assistants.md, lots A3 and A4):
// the request's body, a proposal placed on the graph as it stands now and applied through the editor's own
// edit functions, the findings the check gives the proposed graph, and the run's facts sent as `run`.
//
// Nothing here adds or deletes a node (the agent's schema has no such change and this module has no
// such function): a proposal changes the distribution and arguments of an existing prior, an
// expression, a bound or a form. A proposal is placed by node id and by the slot names of the Go
// validator (`prior.sigma`, `terms[0].coef`, `<node>.<component>` with `mean` or `scale`,
// `<node>.correlation` with `eta`); a change whose node or slot is no longer there, or whose «before»
// is no longer what the graph holds, is not applied (a graph the person changed after the question is
// never overwritten).
//
// Pure functions: the tests import them as they are. Every export starts with `bayes`, `BAYES_` or `Bayes`
// (a composable's exports are auto-imported everywhere).
import { bayesDistribution, bayesLimit } from '~/composables/bayesCatalog'
import { bayesPriorText } from '~/composables/bayesEditorText'
import { bayesNodeSlots, bayesSetDist, bayesSetForm, bayesSetStateForm, bayesWriteSlot, type BayesDoc, type BayesSlotPlace } from '~/composables/bayesGraphEdits'
import type { BayesGraphFinding, BayesPriorPlan } from '~/composables/bayesGraph'

// ---------------------------------------------------------------- the request

/** What the assistant says back at the end of a turn (`done.proposal`). */
export interface BayesChange {
  node: string
  field: 'prior_distribution' | 'prior_args' | 'expression' | 'bounds' | 'plate_form'
  /** For `expression` only: which expression of the node, in the Go validator's keys. */
  slot: string
  before: unknown
  value: unknown
  why: string
}
export interface BayesProposal { summary: string, changes: BayesChange[] }

/** The facts of a run the person chose (`GET /bayes-models/{id}/runs/{run}/facts`); the agent keeps its own keys. */
export interface BayesRunFacts {
  run_id?: number
  version: number
  sampler: Record<string, number>
  nodes: Array<{ node: string, rhat_max?: number | null, ess_bulk_min?: number | null, ess_tail_min?: number | null }>
}

/** One entry of `GET /bayes-models/{id}/runs`: a Bayesian node of a flow that pins the model. */
export interface BayesRunListed {
  flow_id: number
  flow_name: string
  node_id: string
  node_label: string
  pinned_version: number
  run_id: number | null
  version: number | null
  trial: boolean
  finished_at: string | null
}

export interface BayesAssistInput {
  message: string
  model: string
  chatId: number | null
  doc: BayesDoc
  findings: readonly BayesGraphFinding[]
  /** A finding's message in the person's words (the editor's); the code stands without it. */
  say?: (f: BayesGraphFinding) => string
  selected: string | null
  preview?: Record<string, Record<string, number | null>>
  run?: BayesRunFacts | null
  /** The version the editor holds (null: never saved). */
  version: number | null
  /** Whether the graph is the one saved. */
  saved: boolean
  locale: string
}

const MAX_FINDINGS = 200

/** The graph as the assistant reads it: the nodes and the boxes, not the drawing (the layout is not a fact of the model). */
export function bayesAssistGraph (doc: BayesDoc): Record<string, unknown> {
  return { format_version: doc.format_version, plates: doc.plates, nodes: doc.nodes }
}

/** The graph's size as the gateway measures it: UTF-8 bytes of its compact JSON. */
export function bayesAssistGraphBytes (doc: BayesDoc): number {
  return new TextEncoder().encode(JSON.stringify(bayesAssistGraph(doc))).length
}

/** Whether the graph fits in what the assistant takes (`max_graph_bytes`). */
export function bayesAssistFits (doc: BayesDoc): boolean {
  return bayesAssistGraphBytes(doc) <= bayesLimit('max_graph_bytes')
}

/** The body of `POST /ai/bayes-assist`. */
export function bayesAssistBody (i: BayesAssistInput): Record<string, unknown> {
  return {
    message: i.message,
    model: i.model,
    chat_id: i.chatId,
    graph: bayesAssistGraph(i.doc),
    findings: i.findings.slice(0, MAX_FINDINGS).map(f => ({
      code: f.code,
      severity: f.severity,
      node: f.node,
      ...(f.slot ? { slot: f.slot } : {}),
      ...(i.say ? { message: i.say(f) } : {}),
    })),
    selected: i.selected,
    preview: i.preview ?? {},
    run: i.run ? bayesRunSent(i.run) : null,
    version: i.version,
    saved: i.saved,
    locale: i.locale,
  }
}

/**
 * The numbers the browser computes for the selected node's prior (the quantiles the chart shows): one entry
 * per quantity of the plan that belongs to the node, never more than `max`. `draw` is the preview's own
 * function (the worker's); a quantity it cannot draw is left out.
 */
export async function bayesAssistPreview (
  plan: BayesPriorPlan | null, selected: string | null,
  draw: (target: string) => Promise<{ q5: number, q50: number, q95: number } | null> | { q5: number, q50: number, q95: number } | null, max = 12,
): Promise<Record<string, Record<string, number>>> {
  const out: Record<string, Record<string, number>> = {}
  if (!plan || !selected) return out
  for (const id of plan.order) {
    if (Object.keys(out).length >= max) break
    if (id !== selected && !id.startsWith(`${selected}.`)) continue
    let q: Awaited<ReturnType<typeof draw>> = null
    try { q = await draw(id) } catch { q = null }
    if (q && [q.q5, q.q50, q.q95].every(Number.isFinite)) out[id] = { q5: q.q5, q50: q.q50, q95: q.q95 }
  }
  return out
}

// ---------------------------------------------------------------- reading a run

/** What of a run's facts is sent: the sampler's numbers and each node's extremes, nothing else. */
export function bayesRunSent (f: BayesRunFacts): BayesRunFacts {
  const sampler: Record<string, number> = {}
  for (const [k, v] of Object.entries(f.sampler ?? {})) if (typeof v === 'number' && Number.isFinite(v)) sampler[k] = v
  const nodes = (f.nodes ?? []).filter(n => typeof n?.node === 'string').map(n => ({
    node: n.node,
    ...(typeof n.rhat_max === 'number' ? { rhat_max: n.rhat_max } : {}),
    ...(typeof n.ess_bulk_min === 'number' ? { ess_bulk_min: n.ess_bulk_min } : {}),
    ...(typeof n.ess_tail_min === 'number' ? { ess_tail_min: n.ess_tail_min } : {}),
  }))
  return { version: f.version, sampler, nodes }
}

/** An entry of the «Read a run» picker. A node that was never run is listed and cannot be chosen. */
export interface BayesRunChoice {
  /** `<flow>/<node>` */
  key: string
  entry: BayesRunListed
  disabled: boolean
  /** A development run on a sample (it says so beside the entry). */
  trial: boolean
}

export function bayesRunChoices (list: readonly BayesRunListed[]): BayesRunChoice[] {
  return list.map(entry => ({
    key: `${entry.flow_id}/${entry.node_id}`,
    entry,
    disabled: entry.run_id === null || entry.run_id === undefined,
    trial: !!entry.trial,
  }))
}

/** Whether a run was made on another version than the one the editor holds (the tab says so). */
export function bayesRunOtherVersion (runVersion: number | null | undefined, editorVersion: number | null | undefined): boolean {
  return typeof runVersion === 'number' && typeof editorVersion === 'number' && runVersion !== editorVersion
}

// ---------------------------------------------------------------- placing a proposal

/** Where a unit of a proposal stands against the graph as it is now. */
export type BayesPlace =
  /** it can be applied */
  | 'ok'
  /** the node, the box or the slot is not in the graph any more (or never was that kind) */
  | 'missing'
  /** the graph holds something else than what the assistant read: the person changed it since */
  | 'moved'
  /** the graph already holds the value */
  | 'same'

/** What a unit changes, for the words of the tab. */
export type BayesUnitKind = 'prior' | 'expression' | 'bounds' | 'form'

/**
 * One thing the person may apply: a prior (its distribution and its arguments go together, so they are one
 * unit), an expression, the bounds of a parameter, the form of a box or of a state.
 */
export interface BayesUnit {
  key: string
  kind: BayesUnitKind
  /** The id the change names (a node, a box, `<node>.<component>`, `<node>.correlation`). */
  node: string
  changes: BayesChange[]
  place: BayesPlace
  /** An expression's slot, for its words. */
  slot: BayesSlotPlace | null
  before: string
  after: string
  why: string
}

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const text = (v: unknown): string => (v === null || v === undefined ? '' : String(v).trim())

/** A value `before` as the editor's slot text: an expression is itself, a reference is the id it names. */
function beforeText (v: unknown): string {
  if (isObject(v)) return text(v.expr ?? v.ref)
  return text(v)
}

/** The node an id names, whole or as a part (`<node>.<component>`, `<node>.correlation`). */
function ownerOf (doc: BayesDoc, id: string): { id: string, part: string | null } | null {
  if (doc.nodes.some(n => n.id === id)) return { id, part: null }
  const dot = id.indexOf('.')
  if (dot > 0) {
    const node = doc.nodes.find(n => n.id === id.slice(0, dot))
    const part = id.slice(dot + 1)
    if (node && ((part === 'correlation' && node.correlation) || node.components?.some(c => c.id === part))) return { id: node.id, part }
  }
  return null
}

/** The slot an `expression` change names, in the Go validator's keys (the older `coef:<i>` and `arg:<p>` are read as well). */
function slotKeyOf (slot: string): string {
  const s = slot.trim()
  const coef = /^coef:(\d+)$/.exec(s)
  if (coef) return `terms[${coef[1]}].coef`
  const arg = /^arg:(\w+)$/.exec(s)
  if (arg) return `args.${arg[1]}`
  return s
}

function slotPlaceOf (doc: BayesDoc, c: BayesChange): { place: BayesSlotPlace, owner: string } | null {
  const o = ownerOf(doc, c.node)
  if (!o) return null
  const key = slotKeyOf(c.slot)
  const place = bayesNodeSlots(doc, o.id).find(p => !p.data && p.node === c.node && p.slot === key)
  return place ? { place, owner: o.id } : null
}

function priorPart (doc: BayesDoc, id: string): { part: 'prior' | 'init', dist: string, args: Record<string, { expr?: string, ref?: string }> } | null {
  const n = doc.nodes.find(x => x.id === id)
  if (!n) return null
  const p = n.kind === 'parameter' ? n.prior : n.kind === 'state' ? n.init : undefined
  return p ? { part: n.kind === 'parameter' ? 'prior' : 'init', dist: String(p.dist), args: p.args ?? {} } : null
}

const argText = (s: { expr?: string, ref?: string } | undefined): string => text(s?.expr ?? s?.ref)

function argsText (dist: string, args: Record<string, unknown>): string {
  const order = bayesDistribution(dist)?.params.map(p => p.name) ?? []
  return bayesPriorText(dist, Object.fromEntries(Object.entries(args).map(([k, v]) => [k, { expr: text(v) }])), order)
}

/** The units of a proposal: a prior's two changes are one, every other change is its own. */
export function bayesProposalUnits (doc: BayesDoc, proposal: BayesProposal): BayesUnit[] {
  const units: BayesUnit[] = []
  const priors = new Map<string, BayesUnit>()
  for (const c of proposal.changes) {
    if (c.field === 'prior_distribution' || c.field === 'prior_args') {
      let u = priors.get(c.node)
      if (!u) {
        u = { key: `prior:${c.node}`, kind: 'prior', node: c.node, changes: [], place: 'missing', slot: null, before: '', after: '', why: '' }
        priors.set(c.node, u)
        units.push(u)
      }
      u.changes.push(c)
      if (c.why) u.why = u.why && !u.why.includes(c.why) ? `${u.why} ${c.why}` : (u.why || c.why)
    } else {
      units.push({ key: `${c.field}:${c.node}:${c.slot}`, kind: c.field === 'expression' ? 'expression' : c.field === 'bounds' ? 'bounds' : 'form', node: c.node, changes: [c], place: 'missing', slot: null, before: '', after: '', why: c.why })
    }
  }
  for (const u of units) placeUnit(doc, u)
  return units
}

/** Fills a unit's place and its before and after, against the graph now. */
function placeUnit (doc: BayesDoc, u: BayesUnit): void {
  u.place = 'missing'
  if (u.kind === 'prior') {
    const cur = priorPart(doc, u.node)
    if (!cur) return
    const dist = u.changes.find(c => c.field === 'prior_distribution')
    const args = u.changes.find(c => c.field === 'prior_args')
    const newDist = dist ? text(dist.value) : cur.dist
    const params = bayesDistribution(newDist)?.params.map(p => p.name)
    if (!params || (dist && !args) || !args || !isObject(args.value)) return
    const given = Object.keys(args.value)
    if (given.length !== params.length || !given.every(k => params.includes(k))) return
    const nowArgs = Object.fromEntries(Object.entries(cur.args).map(([k, v]) => [k, argText(v)]))
    u.before = argsText(cur.dist, nowArgs)
    u.after = argsText(newDist, args.value)
    // what the assistant read must be what the graph holds
    const read = isObject(args.before) ? args.before : {}
    const readDist = dist ? text(dist.before) : cur.dist
    const sameRead = readDist === cur.dist
      && Object.entries(read).every(([k, v]) => beforeText(v) === (nowArgs[k] ?? ''))
      && Object.keys(nowArgs).every(k => k in read)
    const sameAfter = newDist === cur.dist && given.every(k => text((args.value as Record<string, unknown>)[k]) === (nowArgs[k] ?? ''))
    u.place = sameAfter ? 'same' : sameRead ? 'ok' : 'moved'
    return
  }
  const c = u.changes[0]!
  if (u.kind === 'expression') {
    const found = slotPlaceOf(doc, c)
    if (!found) return
    u.slot = found.place
    u.before = found.place.text
    u.after = text(c.value)
    u.place = !u.after ? 'missing' : u.after === found.place.text ? 'same' : beforeText(c.before) === found.place.text ? 'ok' : 'moved'
    return
  }
  if (u.kind === 'bounds') {
    const n = doc.nodes.find(x => x.id === c.node)
    if (!n || n.kind !== 'parameter' || !isObject(c.value)) return
    const now = isObject(n.bounds) ? n.bounds as Record<string, unknown> : {}
    const keep = (v: unknown): number | null => (typeof v === 'number' ? v : null)
    const want: Record<'lower' | 'upper', number | null> = { lower: keep(now.lower), upper: keep(now.upper) }
    for (const k of ['lower', 'upper'] as const) if (k in c.value) want[k] = keep((c.value as Record<string, unknown>)[k])
    u.before = boundsText(keep(now.lower), keep(now.upper))
    u.after = boundsText(want.lower, want.upper)
    const read = isObject(c.before) ? c.before as Record<string, unknown> : {}
    const sameRead = keep(read.lower) === keep(now.lower) && keep(read.upper) === keep(now.upper)
    u.place = u.before === u.after ? 'same' : sameRead ? 'ok' : 'moved'
    return
  }
  // a form: a group box's or a state's
  const plate = doc.plates.find(p => p.id === c.node)
  const node = doc.nodes.find(x => x.id === c.node)
  const holder = plate?.kind === 'group' ? plate : node?.kind === 'state' ? node : null
  if (!holder || (c.value !== 'centred' && c.value !== 'non_centred')) return
  const now = holder.form === 'centred' ? 'centred' : 'non_centred'
  u.before = now
  u.after = String(c.value)
  u.place = now === c.value ? 'same' : text(c.before) === now ? 'ok' : 'moved'
}

function boundsText (lower: number | null, upper: number | null): string {
  if (lower !== null && upper !== null) return `${lower} … ${upper}`
  if (lower !== null) return `≥ ${lower}`
  if (upper !== null) return `≤ ${upper}`
  return ''
}

/**
 * Applies the units that can be applied to `doc`, in place, with the editor's own edit functions, and returns
 * how many were. Nothing else is touched: no node or box is added or removed.
 */
export function bayesApplyUnits (doc: BayesDoc, units: readonly BayesUnit[]): number {
  let applied = 0
  for (const u of units) {
    if (u.place !== 'ok') continue
    if (applyUnit(doc, u)) applied++
  }
  return applied
}

function applyUnit (doc: BayesDoc, u: BayesUnit): boolean {
  if (u.kind === 'prior') {
    const cur = priorPart(doc, u.node)
    const dist = u.changes.find(c => c.field === 'prior_distribution')
    const args = u.changes.find(c => c.field === 'prior_args')
    if (!cur || !args || !isObject(args.value)) return false
    if (dist) bayesSetDist(doc, u.node, cur.part, text(dist.value))
    for (const [k, v] of Object.entries(args.value)) bayesWriteSlot(doc, u.node, `${cur.part}.${k}`, { expr: text(v) })
    return true
  }
  const c = u.changes[0]!
  if (u.kind === 'expression') {
    const found = slotPlaceOf(doc, c)
    return !!found && bayesWriteSlot(doc, found.owner, found.place.path, { expr: text(c.value) })
  }
  if (u.kind === 'bounds') {
    const n = doc.nodes.find(x => x.id === c.node)
    if (!n || !isObject(c.value)) return false
    const b: Record<string, number> = { ...(isObject(n.bounds) ? n.bounds as Record<string, number> : {}) }
    for (const k of ['lower', 'upper'] as const) {
      if (!(k in c.value)) continue
      const v = (c.value as Record<string, unknown>)[k]
      if (typeof v === 'number' && Number.isFinite(v)) b[k] = v
      else delete b[k]
    }
    if (Object.keys(b).length) n.bounds = b
    else delete n.bounds
    return true
  }
  const nonCentred = c.value === 'non_centred'
  if (doc.plates.some(p => p.id === c.node && p.kind === 'group')) bayesSetForm(doc, c.node, nonCentred)
  else bayesSetStateForm(doc, c.node, nonCentred)
  return true
}

/** The graph with these units applied, as a copy: what the check reads before «Apply» is offered. */
export function bayesProposedGraph (doc: BayesDoc, units: readonly BayesUnit[]): BayesDoc {
  const next = clone(doc)
  bayesApplyUnits(next, units)
  return next
}

/** A finding as a key, to tell what a proposal adds from what the graph already had. */
export const bayesFindingKey = (f: Pick<BayesGraphFinding, 'code' | 'node' | 'slot'>): string => `${f.code}|${f.node ?? ''}|${f.slot ?? ''}`

/** The findings of the proposed graph that the graph on the canvas does not have. */
export function bayesNewFindings (now: readonly BayesGraphFinding[], proposed: readonly BayesGraphFinding[]): BayesGraphFinding[] {
  const had = new Set(now.map(bayesFindingKey))
  return proposed.filter(f => !had.has(bayesFindingKey(f)))
}

/**
 * What the check says of a proposed graph: the findings it adds, and whether it may be offered (no new
 * error). The findings the graph already had are the editor's own and are not the proposal's.
 */
export function bayesVerdictOf (now: readonly BayesGraphFinding[], proposed: readonly BayesGraphFinding[]): { added: BayesGraphFinding[], applicable: boolean } {
  const added = bayesNewFindings(now, proposed)
  return { added, applicable: !added.some(f => f.severity === 'error') }
}

/** Whether a unit is still to be offered: it can be placed, and applying it changes something. */
export const bayesUnitOpen = (u: Pick<BayesUnit, 'place'>): boolean => u.place === 'ok'

/** A proposal read from `done`, or null when it holds no change (never trusts the shape of what arrives). */
export function bayesReadProposal (raw: unknown): BayesProposal | null {
  if (!isObject(raw) || !Array.isArray(raw.changes)) return null
  const FIELDS = ['prior_distribution', 'prior_args', 'expression', 'bounds', 'plate_form']
  const changes: BayesChange[] = []
  for (const c of raw.changes) {
    if (!isObject(c) || typeof c.node !== 'string' || typeof c.field !== 'string' || !FIELDS.includes(c.field)) continue
    changes.push({
      node: c.node,
      field: c.field as BayesChange['field'],
      slot: typeof c.slot === 'string' ? c.slot : '',
      before: c.before,
      value: c.value,
      why: typeof c.why === 'string' ? c.why : '',
    })
  }
  return changes.length ? { summary: typeof raw.summary === 'string' ? raw.summary : '', changes } : null
}
