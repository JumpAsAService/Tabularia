// The name the editor of Bayesian models proposes for a new node (docs/specs/bayes-graph-editor.md,
// «The palette»): made from its label as the graph spec says — letters folded to ASCII, lower case,
// any other character to `_` — then made unique with a number. The person may change it or remove it;
// the mirror's rules (graph.name, graph.name_taken, graph.name_function) stay the authority, and a
// proposal always passes them.
//
// The ids of new nodes and boxes are made here too: an id is the graph's internal key (lower-case
// letters and digits, at most 16), never shown, never typed.
//
// Pure functions, nothing of Vue, Nuxt or the DOM. Every export starts with `bayes` or `BAYES_`: a
// composable's exports are auto-imported everywhere.
import { BAYES_CATALOG, bayesLimit, type BayesCatalog } from '~/composables/bayesCatalog'

/** The rule of a name (the graph spec): lower-case letters, digits and `_`, starting with a letter. */
export const BAYES_NAME_RULE = /^[a-z][a-z0-9_]*$/

/** A proposal is kept short enough to read on a node: cut at a word under this many characters. */
export const BAYES_NAME_PROPOSAL_CHARS = 24

// Letters NFKD does not take apart, and the Greek letters a statistician writes (μ is `mu`).
const LETTERS: Record<string, string> = {
  ß: 'ss', æ: 'ae', œ: 'oe', ø: 'o', ł: 'l', đ: 'd', ð: 'd', þ: 'th', ı: 'i', ĳ: 'ij',
  α: 'alpha', β: 'beta', γ: 'gamma', δ: 'delta', ε: 'epsilon', ζ: 'zeta', η: 'eta', θ: 'theta', ι: 'iota',
  κ: 'kappa', λ: 'lambda', μ: 'mu', µ: 'mu', ν: 'nu', ξ: 'xi', ο: 'omicron', π: 'pi', ρ: 'rho', σ: 'sigma',
  ς: 'sigma', τ: 'tau', υ: 'upsilon', φ: 'phi', χ: 'chi', ψ: 'psi', ω: 'omega',
}

/** A label folded to the alphabet of a name: `Livello della scuola` → `livello_della_scuola`, '' when nothing is left. */
export function bayesNameFromLabel (label: string, maxChars = BAYES_NAME_PROPOSAL_CHARS): string {
  let out = ''
  for (const ch of label.toLowerCase()) {
    const mapped = LETTERS[ch]
    if (mapped) { out += mapped; continue }
    // a letter with its accents taken apart keeps its base letter (é → e); the rest becomes `_`
    const base = ch.normalize('NFKD').replace(/\p{M}/gu, '')
    out += /^[a-z0-9]+$/.test(base) ? base : '_'
  }
  let name = out.replace(/_+/g, '_').replace(/^_+|_+$/g, '')
  if (!name) return ''
  if (!/^[a-z]/.test(name)) name = `v_${name}`
  if (name.length > maxChars) {
    // cut at the last word that fits, never inside a word when a word boundary exists
    const cut = name.slice(0, maxChars + 1)
    const at = cut.lastIndexOf('_')
    name = (at > 0 ? cut.slice(0, at) : name.slice(0, maxChars)).replace(/_+$/, '')
  }
  return name
}

/** Whether a name passes the graph's own rules but for being unique (graph.name, graph.name_function). */
export function bayesNameAllowed (name: string, catalog: BayesCatalog = BAYES_CATALOG): boolean {
  if (!BAYES_NAME_RULE.test(name) || name.endsWith('__')) return false
  if (new TextEncoder().encode(name).length > bayesLimit('max_name_bytes', catalog)) return false
  return !catalog.language.functions.some(f => f.name === name)
}

/**
 * A name from a label, unique among `taken`: `school`, then `school_2`, `school_3`… A label with
 * nothing to fold (only symbols, another alphabet) gives `fallback`, numbered the same way.
 */
export function bayesUniqueName (label: string, taken: ReadonlySet<string>, fallback = 'node', catalog: BayesCatalog = BAYES_CATALOG): string {
  const base = bayesNameFromLabel(label) || bayesNameFromLabel(fallback) || 'node'
  const ok = (n: string) => bayesNameAllowed(n, catalog) && !taken.has(n)
  if (ok(base)) return base
  for (let i = 2; ; i++) {
    const n = `${base}_${i}`
    if (ok(n)) return n
  }
}

/** What a graph document holds that a name or an id must not repeat. */
export interface BayesNamedDoc {
  plates: ReadonlyArray<{ id: string }>
  nodes: ReadonlyArray<{ id: string, name?: string | null, components?: ReadonlyArray<{ id: string, name: string }>, correlation?: { name: string } }>
}

/** Every name the document holds: the nodes', their components' and their correlations'. */
export function bayesTakenNames (doc: BayesNamedDoc, except?: string): Set<string> {
  const out = new Set<string>()
  for (const n of doc.nodes) {
    if (n.id === except) continue
    if (n.name) out.add(n.name)
    for (const c of n.components ?? []) if (c.name) out.add(c.name)
    if (n.correlation?.name) out.add(n.correlation.name)
  }
  return out
}

// An id must not equal a reserved suffix without its underscore (the graph's rule graph.id).
function reservedIds (catalog: BayesCatalog): Set<string> {
  return new Set(((catalog.stan.reserved_suffixes ?? []) as string[]).map(x => x.replace(/^_/, '')))
}

/** A new id with this prefix (`n`, `p`, `c`), unique among the document's plates and nodes: `n1`, `n2`… */
export function bayesNewId (doc: BayesNamedDoc, prefix: string, catalog: BayesCatalog = BAYES_CATALOG): string {
  const used = new Set<string>(['rows', ...doc.plates.map(p => p.id), ...doc.nodes.map(n => n.id)])
  for (const n of doc.nodes) for (const c of n.components ?? []) used.add(c.id)
  const reserved = reservedIds(catalog)
  for (let i = 1; ; i++) {
    const id = `${prefix}${i}`
    if (!used.has(id) && !reserved.has(id)) return id
  }
}
