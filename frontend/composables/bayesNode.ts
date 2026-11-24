// The rules of the node «Bayesian model» that need no browser (docs/specs/bayes-flow-node.md, lot F3): the data a new node
// starts with, the columns a data variable of the model accepts, the move to a newer version (which bindings it keeps), the
// notice «a newer version exists», what the pinned version can check, and what the sampler's fields accept. The panel
// (components/bayes/BayesNodePanel.vue) and the canvas node (components/nodes/BayesModelNode.vue) only draw them; the gateway
// checks everything again at the run (`Task.Check`, `planBayes`).
// Exports start with `bayesNode`: a composable's exports are auto-imported everywhere.
import { bayesGraphReadText, type BayesBinding, type BayesModel } from '~/composables/bayesGraph'
import { modelColumnKind } from '~/composables/modelCatalog'

export const BAYES_NODE_TYPE = 'bayes_model'

/** The sampler's fields: default, range (the contract's `Task.Check`) and the step the field takes. */
export const BAYES_NODE_SAMPLER = {
  chains: { default: 4, min: 1, max: 8 },
  warmup: { default: 1000, min: 0, max: 10_000 },
  draws: { default: 1000, min: 1, max: 10_000 },
  adapt_delta: { default: 0.8, min: 0, max: 1 }, // open at both ends
  max_treedepth: { default: 10, min: 1, max: 15 },
} as const

/** The seed's range: drawn once for a new node (1 … 2³¹ − 1). */
export const BAYES_NODE_SEED_MAX = 2_147_483_647

/** The node's own limits: the default timeout (sampling is not a model node's 900 s), the shortest one and the most retries. */
export const BAYES_NODE_TIMEOUT = { default: 1800, min: 10 }
export const BAYES_NODE_RETRIES_MAX = 5
export const BAYES_NODE_REPLICATED_DEFAULT = 50

/** The group's limits as its recipe has them (the group `bayes`); an administrator may have set others, which only the administration card shows. */
export const BAYES_NODE_GROUP_DEFAULTS = {
  memory: '2 GiB', cpu: '4', tmp: '2 GiB', max_rows: 1_000_000, max_quantities: 20_000, max_units: 20_000, max_seconds: 3600,
} as const

export const BAYES_NODE_OUTPUTS = ['summary', 'draws', 'replicated', 'results'] as const
export type BayesNodeOutput = typeof BAYES_NODE_OUTPUTS[number]

export type BayesNodeInit = null | number | Record<string, number>

export interface BayesNodeSampler {
  chains: number
  warmup: number
  draws: number
  seed: number
  adapt_delta: number
  max_treedepth: number
  init: BayesNodeInit
}

/** A seed for a new node: an integer in 1 … 2³¹ − 1. */
export function bayesNodeSeed(random: () => number = Math.random): number {
  return 1 + Math.floor(random() * BAYES_NODE_SEED_MAX)
}

/** The data of a new node: no model yet, the sampler's defaults and a seed of its own. */
export function bayesNodeData(seed: number = bayesNodeSeed()): Record<string, any> {
  const s = BAYES_NODE_SAMPLER
  return {
    bayesModelId: null,
    bayesModelVersion: null,
    bindings: {},
    sampler: { chains: s.chains.default, warmup: s.warmup.default, draws: s.draws.default, seed, adapt_delta: s.adapt_delta.default, max_treedepth: s.max_treedepth.default, init: null },
    output: 'summary',
    replicatedDraws: BAYES_NODE_REPLICATED_DEFAULT,
    timeoutSeconds: BAYES_NODE_TIMEOUT.default,
    retries: 0,
    retryDelaySeconds: 60,
  }
}

// ── the variables and the columns they take ──────────────────────────────────

export type BayesNodeVariableType = 'real' | 'int' | 'group_index' | 'category' | 'time'
const BINDABLE: readonly string[] = ['real', 'int', 'group_index', 'category', 'time']

/** The data nodes a column is bound to: the numbers, the integers, the groups' indices, the categories and the time. */
export const bayesNodeBindable = (bindings: readonly BayesBinding[]): BayesBinding[] => bindings.filter((b) => BINDABLE.includes(b.type))

/** The mirror's reading of a stored graph (the version's `graph`, an object or its text); null when it cannot be read. */
export function bayesNodeRead(graphJSON: unknown): BayesModel | null {
  if (!graphJSON) return null
  return bayesGraphReadText(typeof graphJSON === 'string' ? graphJSON : JSON.stringify(graphJSON)).model
}

/** The data nodes of a stored graph a column is bound to. */
export const bayesNodeVariables = (graphJSON: unknown): BayesBinding[] => bayesNodeBindable(bayesNodeRead(graphJSON)?.bindings ?? [])

/** The kinds of column (`modelColumnKind`'s words) a variable takes: numbers for a real or an integer (0 or 1: a boolean too),
 *  text and integers for a group's index or a category, dates, timestamps, integers and numbers for the time. */
export function bayesNodeAccepts(v: Pick<BayesBinding, 'type' | 'support'>): string[] {
  switch (v.type) {
    case 'real': return ['integer', 'number']
    case 'int': return v.support === 'binary' ? ['integer', 'number', 'boolean'] : ['integer', 'number']
    case 'group_index': case 'category': return ['text', 'integer']
    case 'time': return ['date', 'datetime', 'integer', 'number']
    default: return []
  }
}

/** Whether a column of that type (as a preview names it) fits the variable. */
export function bayesNodeFits(v: Pick<BayesBinding, 'type' | 'support'>, dtype: string): boolean {
  return bayesNodeAccepts(v).includes(modelColumnKind(dtype))
}

/** The columns of the input the variable takes, in the input's order. */
export function bayesNodeColumnsFor(v: Pick<BayesBinding, 'type' | 'support'>, columns: readonly { name: string, dtype: string }[]): { name: string, dtype: string }[] {
  return columns.filter((c) => bayesNodeFits(v, c.dtype))
}

/** What the person still has to bind: the variables with no column (or a column with no name). */
export function bayesNodeUnbound(variables: readonly BayesBinding[], bindings: Record<string, string> | null | undefined): BayesBinding[] {
  return variables.filter((v) => !String(bindings?.[v.node] ?? '').trim())
}

/** How a variable is called to the person: its label, else its name, else its id. */
export const bayesNodeNameOf = (v: Pick<BayesBinding, 'node' | 'name' | 'label'>): string => v.label || v.name || v.node

// ── the move to another version ──────────────────────────────────────────────

export interface BayesNodeMove {
  bindings: Record<string, string>
  /** the data nodes whose column was kept */
  kept: string[]
  /** the data nodes whose column was cleared because its type no longer fits the variable */
  cleared: BayesBinding[]
  /** the variables of the new version with no column afterwards: the new ones and the cleared ones */
  unbound: BayesBinding[]
}

/** The bindings after the pin moves to another version: a data node still there keeps its column (bindings are by the node's id,
 *  stable across versions) when the column still fits the variable's type, or when the input's columns are not known; the
 *  others are cleared. A column the input no longer has is kept: the panel shows it struck. */
export function bayesNodeMove(
  bindings: Record<string, string> | null | undefined,
  next: readonly BayesBinding[],
  columns: readonly { name: string, dtype: string }[],
): BayesNodeMove {
  const out: Record<string, string> = {}
  const kept: string[] = []
  const cleared: BayesBinding[] = []
  for (const v of next) {
    const column = String(bindings?.[v.node] ?? '').trim()
    if (!column) continue
    const found = columns.find((c) => c.name === column)
    if (found && !bayesNodeFits(v, found.dtype)) { cleared.push(v); continue }
    out[v.node] = column
    kept.push(v.node)
  }
  return { bindings: out, kept, cleared, unbound: bayesNodeUnbound(next, out) }
}

// ── «a newer version exists» ─────────────────────────────────────────────────

export interface BayesNodeNewer {
  /** the model's current version */
  version: number
  /** the current version's program differs from the pinned one's (a layout-only version says nothing) */
  changed: boolean
  /** the pinned version is not in the list any more */
  pinnedMissing: boolean
}

/** The notice's rule: the current version is later than the pinned one and its semantic hash differs from the pinned one's. `null`:
 *  nothing to say. `versions` is the list of the model's versions (`BayesVersionListed`). */
export function bayesNodeNewer(
  currentVersion: number,
  pinned: number,
  versions: readonly { version: number, semantic_hash: string }[],
): BayesNodeNewer | null {
  if (!(currentVersion > pinned)) return null
  const mine = versions.find((v) => v.version === pinned)
  const now = versions.find((v) => v.version === currentVersion)
  if (!mine) return { version: currentVersion, changed: true, pinnedMissing: true }
  if (!now) return null
  return { version: currentVersion, changed: now.semantic_hash !== mine.semantic_hash, pinnedMissing: false }
}

/** Whether the notice is shown: a later version that changes the model. */
export const bayesNodeNotice = (n: BayesNodeNewer | null): boolean => !!n && n.changed

// ── what the pinned version can check ────────────────────────────────────────

export type BayesNodeChecks = 'none' | 'posterior' | 'both'

/** From the stored data manifest (never from today's generator): no entry of kind `checks_on` → sampling only (`none`); `checks_on`
 *  with the `prior_count` entries → the posterior checks and the prior predictive check (`both`); `checks_on` without → the
 *  posterior checks only. The manifest is the list of entries, or an object holding it as `data`. */
export function bayesNodeChecks(manifest: unknown): BayesNodeChecks {
  const list = Array.isArray(manifest) ? manifest : Array.isArray((manifest as any)?.data) ? (manifest as any).data : []
  const kinds = new Set(list.map((e: any) => e?.kind))
  if (!kinds.has('checks_on')) return 'none'
  return kinds.has('prior_count') ? 'both' : 'posterior'
}

// ── the initial values ───────────────────────────────────────────────────────

/** The parameters the person may give a starting value: the single-number quantities the graph declares with a prior (`names`:
 *  the node ids of the graph's parameters); a vector's values are CmdStan's. From the stored quantity manifest. */
export function bayesNodeInitTargets(
  manifest: unknown,
  parameters: Iterable<string>,
): { stan: string, node: string }[] {
  const names = new Set(parameters)
  const list = Array.isArray(manifest) ? manifest : Array.isArray((manifest as any)?.quantities) ? (manifest as any).quantities : []
  const out: { stan: string, node: string }[] = []
  for (const q of list as any[]) {
    if (q?.role !== 'quantity' || !q?.stan || !q?.node || (q.dims ?? []).length > 0 || !names.has(q.node)) continue
    out.push({ stan: String(q.stan), node: String(q.node) })
  }
  return out
}

export type BayesNodeInitMode = 'default' | 'radius' | 'values'

/** What `init` is: CmdStan's own, a radius or values. */
export const bayesNodeInitMode = (init: BayesNodeInit | undefined): BayesNodeInitMode => (typeof init === 'number' ? 'radius' : init && typeof init === 'object' ? 'values' : 'default')

/** The `init` after one parameter's field changed: a number sets it, an empty text clears it; no value left is `null`. */
export function bayesNodeInitWith(init: BayesNodeInit | undefined, stan: string, text: string): BayesNodeInit {
  const own: Record<string, number> = init && typeof init === 'object' ? { ...init } : {}
  const x = Number(text)
  if (text.trim() === '' || !Number.isFinite(x)) delete own[stan]
  else own[stan] = x
  return Object.keys(own).length ? own : null
}

/** The initial values after the pin moved: the parameters the new version still has keep theirs, a radius stays; none left is `null`. */
export function bayesNodeInitKeep(init: BayesNodeInit | undefined, targets: readonly { stan: string }[]): BayesNodeInit {
  if (typeof init === 'number') return init
  if (!init || typeof init !== 'object') return null
  const names = new Set(targets.map((t) => t.stan))
  const own = Object.fromEntries(Object.entries(init).filter(([k]) => names.has(k)))
  return Object.keys(own).length ? own : null
}

// ── the fields' values ───────────────────────────────────────────────────────

/** A whole number in the field's range (the nearest end outside it), or `fallback` when the text is no number. */
export function bayesNodeWhole(text: string, min: number, max: number, fallback: number): number {
  const x = Math.round(Number(text))
  if (text.trim() === '' || !Number.isFinite(x)) return fallback
  return Math.min(max, Math.max(min, x))
}

/** `adapt_delta` strictly between 0 and 1: the text outside is the nearest value inside, a text that is no number is `fallback`. */
export function bayesNodeDelta(text: string, fallback: number): number {
  const x = Number(text)
  if (text.trim() === '' || !Number.isFinite(x)) return fallback
  return Math.min(0.999, Math.max(0.001, x))
}

/** The sampler as the node holds it, with the defaults for what is missing (a node saved by an older interface). */
export function bayesNodeSamplerOf(raw: Record<string, any> | null | undefined, seed: number = 1): BayesNodeSampler {
  const s = BAYES_NODE_SAMPLER
  const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d)
  return {
    chains: num(raw?.chains, s.chains.default), warmup: num(raw?.warmup, s.warmup.default), draws: num(raw?.draws, s.draws.default),
    seed: num(raw?.seed, seed), adapt_delta: num(raw?.adapt_delta, s.adapt_delta.default), max_treedepth: num(raw?.max_treedepth, s.max_treedepth.default),
    init: raw?.init === undefined ? null : raw.init,
  }
}

/** The replicated draws a chain keep: 1 … draws. */
export const bayesNodeReplicated = (value: unknown, draws: number): number => bayesNodeWhole(String(value ?? ''), 1, Math.max(1, draws), Math.min(BAYES_NODE_REPLICATED_DEFAULT, Math.max(1, draws)))
