// The templates of the Bayesian editor as the browser reads them: ten models drawn as graphs of the
// model graph's format, with their default priors written in the open (docs/specs/bayes-templates.md).
// Everything comes from assets/bayes-templates.json, a byte-for-byte copy of the gateway's file
// (gateway-go/internal/bayestemplates/templates.json; the copy by .github/scripts/bayes_templates.py).
//
// bayesOpenTemplate makes a new graph from a template: the labels of the person's language, the
// outcome's typical value and spread written into the priors as visible expressions (`2.5 * 300`,
// `1 / 300`: constants the person typed, never data), and the AR template's order. The result is
// plain graph JSON; nothing in it says it came from a template.
//
// Pure functions, nothing of Nuxt or the DOM: the tests import them as they are. Every export starts
// with `bayes`, `BAYES_` or `Bayes`: a composable's exports are auto-imported everywhere.

import raw from '~/assets/bayes-templates.json'
import { BAYES_CATALOG, bayesLimit, type BayesCatalog } from '~/composables/bayesCatalog'
import { bayesFormatNumber, bayesParseExpr } from '~/composables/bayesExpr'

/** A slot of a graph document: one expression or one reference. */
export interface BayesTemplateSlot { expr?: string, ref?: string }

/** A node of a template's graph, as written in the file (the keys a kind owns, nothing more). */
export interface BayesTemplateNode {
  id: string
  kind: string
  name?: string
  label: string
  prior?: { dist: string, args: Record<string, BayesTemplateSlot> }
  init?: { dist: string, args: Record<string, BayesTemplateSlot> }
  args?: Record<string, BayesTemplateSlot>
  components?: Array<{ id: string, name: string, label: string, mean: BayesTemplateSlot, scale: BayesTemplateSlot }>
  correlation?: { name: string, label: string, eta: BayesTemplateSlot }
  [key: string]: unknown
}

/** A graph document of format 1 (what `Decode` reads in Go and `bayesGraphDecode` in the browser). */
export interface BayesTemplateGraph {
  format_version: number
  plates: Array<{ id: string, kind: string, label: string, [key: string]: unknown }>
  nodes: BayesTemplateNode[]
  layout: Record<string, { x: number, y: number, w?: number, h?: number }>
}

export interface BayesTemplateScaled { node: string, slot: string, as: 'center' | 'spread' | 'rate', times?: number }

export interface BayesTemplateOrder {
  default: number
  node: string
  id_prefix: string
  name_prefix: string
  label: string
  likelihood: string
  slot: string
  outcome: string
  mean: string
  /** The coefficients stand in columns of `per_column`, `row_height` apart, the columns `column_width` apart. */
  per_column: number
  column_width: number
  row_height: number
}

export interface BayesTemplate {
  id: string
  version: number
  family: 'glm' | 'hierarchical' | 'series'
  scale: 'outcome' | null
  scaled: BayesTemplateScaled[]
  sources: Record<string, string>
  order?: BayesTemplateOrder
  graph: BayesTemplateGraph
}

export interface BayesTemplateFile {
  format_version: number
  templates: BayesTemplate[]
  sources: Record<string, { title: string, url: string }>
}

/** What a person gives when opening a template; every field is optional. */
export interface BayesOpenOptions {
  /** The outcome's typical value (0 when not given). */
  center?: number | null
  /** The outcome's typical spread, above 0 (1 when not given). */
  spread?: number | null
  /** The AR order p, from 1 to the catalogue's `max_lag` (the template's own when not given). */
  order?: number | null
}

/** The person's language's words: a function or an object keyed by `bayes.templates.<id>.labels.<node>`. */
export type BayesTemplateTexts = ((key: string) => string | undefined) | Record<string, string | undefined>

/** The real file (the copy of the gateway's). */
export const BAYES_TEMPLATES = raw as unknown as BayesTemplateFile

/** The templates, in the file's order (the empty canvas's order). */
export function bayesTemplates (file: BayesTemplateFile = BAYES_TEMPLATES): BayesTemplate[] {
  return file.templates
}

/** The template with this id, or undefined. */
export function bayesTemplate (id: string, file: BayesTemplateFile = BAYES_TEMPLATES): BayesTemplate | undefined {
  return file.templates.find(t => t.id === id)
}

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T

/** A slot path `prior.<arg>` or `init.<arg>` of a node: the slot's holder and its argument name. */
function slotHolder (node: BayesTemplateNode, path: string): { args: Record<string, BayesTemplateSlot>, arg: string } {
  const [part, arg] = path.split('.')
  const dist = part === 'prior' ? node.prior : part === 'init' ? node.init : undefined
  if (!dist || !arg || !(arg in dist.args)) throw new Error(`template: node ${node.id} has no slot ${path}`)
  return { args: dist.args, arg }
}

function checkNumber (what: string, v: number, catalog: BayesCatalog, positive: boolean): void {
  const max = bayesLimit('max_abs_literal', catalog)
  if (!Number.isFinite(v) || Math.abs(v) > max || (positive && v <= 0)) {
    throw new RangeError(`${what} must be a finite number${positive ? ' above 0' : ''} within ±${max}`)
  }
}

/** The text of a scaled slot for the person's numbers: `1200`, `2.5 * 300`, `1 / 300`. */
export function bayesScaledExpr (s: BayesTemplateScaled, center: number, spread: number): string {
  if (s.as === 'center') return bayesFormatNumber(center)
  if (s.as === 'rate') return `1 / ${bayesFormatNumber(spread)}`
  const times = s.times ?? 1
  return times === 1 ? bayesFormatNumber(spread) : `${bayesFormatNumber(times)} * ${bayesFormatNumber(spread)}`
}

/** The likelihood's mean for the order p: `mu + phi1 * (lag(y, 1) - mu) + …`. */
function orderExpr (o: BayesTemplateOrder, p: number): string {
  const terms = Array.from({ length: p }, (_, i) => `${o.name_prefix}${i + 1} * (lag(${o.outcome}, ${i + 1}) - ${o.mean})`)
  return [o.mean, ...terms].join(' + ')
}

/**
 * The largest AR order a template can be opened with: the catalogue's `max_lag`, or fewer when the
 * likelihood's mean, written as one expression, would pass the language's limits (its bytes, its
 * nodes). 0 for a template with no order.
 */
export function bayesTemplateMaxOrder (tpl: BayesTemplate, catalog: BayesCatalog = BAYES_CATALOG): number {
  if (!tpl.order) return 0
  const max = bayesLimit('max_lag', catalog)
  let best = 0
  for (let p = 1; p <= max; p++) {
    if (!bayesParseExpr(orderExpr(tpl.order, p), catalog).ok) break
    best = p
  }
  return best
}

function applyOrder (g: BayesTemplateGraph, o: BayesTemplateOrder, p: number): void {
  const at = g.nodes.findIndex(n => n.id === o.node)
  if (at < 0) throw new Error(`template: no node ${o.node} to copy for the order`)
  const proto = g.nodes[at]
  const isLag = (id: string): boolean => id.startsWith(o.id_prefix) && /^\d+$/.test(id.slice(o.id_prefix.length))
  const home = g.layout[proto.id] ?? { x: 0, y: 0 }
  const rowsBefore = g.nodes.filter(n => isLag(n.id)).length
  const columnsBefore = Math.ceil(Math.max(rowsBefore, 1) / o.per_column)
  const lags: BayesTemplateNode[] = []
  for (let k = 1; k <= p; k++) {
    const n = clone(proto)
    n.id = `${o.id_prefix}${k}`
    n.name = `${o.name_prefix}${k}`
    n.label = o.label.replace('{k}', String(k))
    lags.push(n)
  }
  g.nodes = [...g.nodes.slice(0, at), ...lags, ...g.nodes.slice(at).filter(n => !isLag(n.id))]
  // The canvas reads left to right: the coefficients fill columns downwards from the first one's
  // place, and whatever stands right of it (the rows' box, the likelihood) moves right by the
  // columns added, so every edge still goes rightwards.
  const shift = (Math.ceil(p / o.per_column) - columnsBefore) * o.column_width
  const layout: BayesTemplateGraph['layout'] = {}
  for (const [id, box] of Object.entries(g.layout)) {
    if (isLag(id)) continue
    layout[id] = box.x > home.x ? { ...box, x: box.x + shift } : box
  }
  lags.forEach((n, i) => {
    layout[n.id] = { x: home.x + o.column_width * Math.floor(i / o.per_column), y: home.y + o.row_height * (i % o.per_column) }
  })
  g.layout = layout
  const lik = g.nodes.find(n => n.id === o.likelihood)
  const [holder, key] = o.slot.split('.')
  const slots = holder === 'args' ? lik?.args : undefined
  if (!slots || !(key in slots)) throw new Error(`template: no slot ${o.slot} on ${o.likelihood}`)
  slots[key] = { expr: orderExpr(o, p) }
}

/**
 * A new graph from a template: a deep copy of its graph at the default numbers, with the AR order
 * (`options.order`, up to `bayesTemplateMaxOrder`), the outcome's numbers written into the slots the template says they rewrite
 * (`options.center`, `options.spread`; 0 and 1 are the defaults and change nothing), and the labels
 * the person's language gives (`texts`, keys `bayes.templates.<id>.labels.<key>` where the key is a
 * node's id, `<node id>_<component id>` for a correlated effect's component, `<node id>_rho` for its
 * correlation, `plate_<plate id>` for a plate, and `phi` with `{k}` for the AR coefficients). A
 * label with no text keeps its English words. Throws a RangeError for a number out of range.
 */
export function bayesOpenTemplate (
  id: string,
  options: BayesOpenOptions = {},
  texts?: BayesTemplateTexts,
  file: BayesTemplateFile = BAYES_TEMPLATES,
  catalog: BayesCatalog = BAYES_CATALOG,
): BayesTemplateGraph {
  const tpl = bayesTemplate(id, file)
  if (!tpl) throw new Error(`no template ${id}`)
  const g = clone(tpl.graph)

  if (tpl.order && options.order != null) {
    const max = bayesTemplateMaxOrder(tpl, catalog)
    if (!Number.isInteger(options.order) || options.order < 1 || options.order > max) {
      throw new RangeError(`the order must be a whole number from 1 to ${max}`)
    }
    applyOrder(g, tpl.order, options.order)
  }

  if (tpl.scale === 'outcome') {
    const center = options.center ?? 0
    const spread = options.spread ?? 1
    checkNumber('the typical value', center, catalog, false)
    checkNumber('the typical spread', spread, catalog, true)
    if (center !== 0 || spread !== 1) {
      for (const s of tpl.scaled) {
        const node = g.nodes.find(n => n.id === s.node)
        if (!node) throw new Error(`template ${tpl.id}: no node ${s.node}`)
        const { args, arg } = slotHolder(node, s.slot)
        if (s.as === 'center' ? center === 0 : spread === 1) continue
        args[arg] = { expr: bayesScaledExpr(s, center, spread) }
      }
    }
  }

  if (texts) {
    const say = (key: string): string | undefined => {
      const v = typeof texts === 'function' ? texts(`bayes.templates.${tpl.id}.labels.${key}`) : texts[`bayes.templates.${tpl.id}.labels.${key}`]
      return v ? v : undefined
    }
    for (const p of g.plates) p.label = say(`plate_${p.id}`) ?? p.label
    for (const n of g.nodes) {
      const lag = tpl.order && n.id.startsWith(tpl.order.id_prefix) && /^\d+$/.test(n.id.slice(tpl.order.id_prefix.length))
      n.label = lag ? (say(tpl.order!.id_prefix)?.replace('{k}', n.id.slice(tpl.order!.id_prefix.length)) ?? n.label) : (say(n.id) ?? n.label)
      for (const c of n.components ?? []) c.label = say(`${n.id}_${c.id}`) ?? c.label
      if (n.correlation) n.correlation.label = say(`${n.id}_rho`) ?? n.correlation.label
    }
  }
  return g
}
