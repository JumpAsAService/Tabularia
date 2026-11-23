// The model graph of the Bayesian editor as the browser reads it: strict decoding, the validator and
// the validated model, a mirror of the gateway's (gateway-go/internal/bayesgraph,
// docs/specs/bayes-model-graph.md, «The TypeScript mirror»). The gateway is the one authority and
// never believes this copy: it gives the person feedback while drawing, and the tests make it give
// the same findings as the Go validator on every case of the shared corpus (code, severity, node,
// slot and arguments, in the same order).
//
// It never computes the canonical form or the semantic hash, nor anything a program depends on.
// It also exports `bayesPriorPlan(model)`, the plan the prior preview draws from
// (docs/specs/bayes-prior-preview.md, «The plan»): plain JSON, no data, no label.
//
// Pure functions, nothing of Nuxt or the DOM. Every export starts with `bayes`, `BAYES_` or
// `Bayes`: a composable's exports are auto-imported everywhere.

import { BAYES_CATALOG, bayesLimit, type BayesCatalog, type BayesDistribution } from '~/composables/bayesCatalog'
import { bayesExprJson, bayesParseExpr, type BayesExprNode } from '~/composables/bayesExpr'
import {
  bayesCheckExpr, bayesIntervalWithin, bayesNamedInterval, bayesTypedInterval, bayesUnbounded,
  type BayesExprInterval, type BayesExprLift, type BayesExprSymbol, type BayesExprType, type BayesExprTyped, type BayesExprEnv,
} from '~/composables/bayesExprCheck'

// ---------------------------------------------------------------- the document

/** One `{expr}` or one `{ref}`; that exactly one is present is a rule (graph.slot), not decoding's. */
export interface BayesGraphSlot { expr: string | null, ref: string | null }
export interface BayesGraphDist { dist: string, args: Record<string, BayesGraphSlot> }
export interface BayesGraphBounds { lower: number | null, upper: number | null }
export interface BayesGraphTerm { data: string, coef: BayesGraphSlot }
export interface BayesGraphComponent { id: string, name: string, label: string, mean: BayesGraphSlot, scale: BayesGraphSlot }
export interface BayesGraphCorrelation { name: string, label: string, eta: BayesGraphSlot }
export interface BayesGraphSeries { time: string, by: string | null }

export interface BayesGraphPlate {
  id: string
  kind: string
  label: string
  note: string
  series: BayesGraphSeries | null
  parent: string | null
  key: string
  form: string
}

/** Any node; absent and null are the same thing (null), an absent string is ''. */
export interface BayesGraphNode {
  id: string
  kind: string
  name: string | null
  label: string
  plate: string | null
  note: string
  type: string
  support: string
  target: string
  standardize: boolean
  prior: BayesGraphDist | null
  bounds: BayesGraphBounds | null
  value: BayesGraphSlot | null
  intercept: BayesGraphSlot | null
  terms: BayesGraphTerm[]
  observed: string
  dist: string
  args: Record<string, BayesGraphSlot>
  process: string
  init: BayesGraphDist | null
  scale: BayesGraphSlot | null
  form: string
  components: BayesGraphComponent[]
  correlation: BayesGraphCorrelation | null
}

export interface BayesGraph {
  formatVersion: number
  plates: BayesGraphPlate[]
  nodes: BayesGraphNode[]
}

export type BayesGraphSeverity = 'error' | 'warning'

export interface BayesGraphFinding {
  code: string
  severity: BayesGraphSeverity
  node: string
  slot: string
  args?: Record<string, unknown>
}

export const BAYES_GRAPH_FORMAT_VERSION = 1

// ---------------------------------------------------------------- small helpers

const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0)
const sortedStrings = (l: Iterable<string>): string[] => [...l].sort(cmp)
const sortedKeys = (s: Set<string>): string[] => sortedStrings(s)
const runeCount = (s: string): number => { let n = 0; for (const _ of s) n++; return n }

function utf8Bytes (s: string): number {
  let n = 0
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i)
    if (c < 0x80) n += 1
    else if (c < 0x800) n += 2
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length && s.charCodeAt(i + 1) >= 0xdc00 && s.charCodeAt(i + 1) <= 0xdfff) { n += 4; i++ } else n += 3
  }
  return n
}

/** Go's json.Marshal of a finding's arguments (keys sorted, <, > and & escaped): the sort key of ties. */
function goJson (v: unknown): string {
  if (v === undefined || v === null) return 'null'
  if (typeof v === 'number') return String(v)
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'string') {
    let out = '"'
    for (const ch of v) {
      const c = ch.codePointAt(0) as number
      if (ch === '"') out += '\\"'
      else if (ch === '\\') out += '\\\\'
      else if (c === 8) out += '\\b'
      else if (c === 9) out += '\\t'
      else if (c === 10) out += '\\n'
      else if (c === 12) out += '\\f'
      else if (c === 13) out += '\\r'
      else if (c < 0x20 || ch === '<' || ch === '>' || ch === '&' || c === 0x2028 || c === 0x2029) out += '\\u' + c.toString(16).padStart(4, '0')
      else out += ch
    }
    return out + '"'
  }
  if (Array.isArray(v)) return '[' + v.map(goJson).join(',') + ']'
  const o = v as Record<string, unknown>
  return '{' + Object.keys(o).sort(cmp).map(k => goJson(k) + ':' + goJson(o[k])).join(',') + '}'
}

/** Orders findings by (node, slot, code), then by their arguments. */
export function bayesSortFindings (fs: BayesGraphFinding[]): void {
  const keys = new Map<BayesGraphFinding, string>()
  const key = (f: BayesGraphFinding) => {
    let k = keys.get(f)
    if (k === undefined) { k = goJson(f.args && Object.keys(f.args).length ? f.args : undefined); keys.set(f, k) }
    return k
  }
  fs.sort((a, b) => cmp(a.node, b.node) || cmp(a.slot, b.slot) || cmp(a.code, b.code) || cmp(key(a), key(b)))
}

const finding = (code: string, severity: BayesGraphSeverity, node: string, slot: string, args?: Record<string, unknown>): BayesGraphFinding =>
  ({ code, severity, node, slot, ...(args ? { args } : {}) })

const tooBig = (what: string, got: number, max: number): BayesGraphFinding =>
  finding('graph.too_big', 'error', '', '', { what, got, max })

const formatFinding = (detail: string, extra?: Record<string, unknown>): BayesGraphFinding =>
  finding('graph.format', 'error', '', '', { detail, ...extra })

// ---------------------------------------------------------------- strict decoding

class JsonFail extends Error {}

/** A number of the document: its value and its text. */
class JNum {
  num: number
  raw: string
  constructor (num: number, raw: string) { this.num = num; this.raw = raw }
}
type JVal = null | boolean | string | JNum | JVal[] | Map<string, JVal>

const MAX_DEPTH = 64

/** Parses JSON, refusing a duplicate key, trailing content and nesting deeper than 64. */
function parseTree (text: string): JVal {
  let i = 0
  const ws = () => { while (i < text.length && (text[i] === ' ' || text[i] === '\t' || text[i] === '\n' || text[i] === '\r')) i++ }
  const bad = (what: string): never => { throw new JsonFail(`invalid JSON: ${what}`) }
  const wellFormed = (s: string) => (typeof (s as any).toWellFormed === 'function' ? (s as any).toWellFormed() as string : s)
  const str = (): string => {
    i++
    let out = ''
    for (;;) {
      if (i >= text.length) bad('unterminated string')
      const c = text[i]
      if (c === '"') { i++; return wellFormed(out) }
      if (c.charCodeAt(0) < 0x20) bad('control character in string')
      if (c === '\\') {
        const e = text[i + 1]
        i += 2
        const simple: Record<string, string> = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t' }
        if (e in simple) out += simple[e]
        else if (e === 'u') {
          const h = text.slice(i, i + 4)
          if (!/^[0-9a-fA-F]{4}$/.test(h)) bad('bad \\u escape')
          out += String.fromCharCode(parseInt(h, 16))
          i += 4
        } else bad('bad escape')
      } else { out += c; i++ }
    }
  }
  const value = (depth: number): JVal => {
    if (depth > MAX_DEPTH) throw new JsonFail(`nested more than ${MAX_DEPTH} levels`)
    ws()
    const c = text[i]
    if (c === undefined) return bad('unexpected end of input')
    if (c === '{') {
      i++
      const m = new Map<string, JVal>()
      ws()
      if (text[i] === '}') { i++; return m }
      for (;;) {
        ws()
        if (text[i] !== '"') bad('object key expected')
        const k = str()
        if (m.has(k)) throw new JsonFail(`duplicate key ${JSON.stringify(k)}`)
        ws()
        if (text[i] !== ':') bad('":" expected')
        i++
        m.set(k, value(depth + 1))
        ws()
        if (text[i] === ',') { i++; continue }
        if (text[i] === '}') { i++; return m }
        bad('"," or "}" expected')
      }
    }
    if (c === '[') {
      i++
      const l: JVal[] = []
      ws()
      if (text[i] === ']') { i++; return l }
      for (;;) {
        l.push(value(depth + 1))
        ws()
        if (text[i] === ',') { i++; continue }
        if (text[i] === ']') { i++; return l }
        bad('"," or "]" expected')
      }
    }
    if (c === '"') return str()
    for (const [w, v] of [['true', true], ['false', false], ['null', null]] as const) {
      if (text.startsWith(w, i)) { i += w.length; return v }
    }
    const m = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/.exec(text.slice(i, i + 400))
    if (!m) return bad('unexpected character')
    i += m[0].length
    return new JNum(Number(m[0]), m[0])
  }
  const v = value(0)
  ws()
  if (i < text.length) throw new JsonFail('unexpected content after the document')
  return v
}

const keySet = (...ks: string[]) => new Set(ks)
const GRAPH_KEYS = keySet('format_version', 'plates', 'nodes', 'layout')
const BOX_KEYS = keySet('x', 'y', 'w', 'h')
const PLATE_COMMON = ['id', 'kind', 'label', 'note']
const PLATE_KEYS: Record<string, Set<string>> = {
  rows: keySet(...PLATE_COMMON, 'series'),
  group: keySet(...PLATE_COMMON, 'parent', 'key', 'form'),
}
const SERIES_KEYS = keySet('time', 'by')
const NODE_COMMON = ['id', 'kind', 'label', 'name', 'plate', 'note']
const SLOT_KEYS = keySet('expr', 'ref')
const DIST_KEYS = keySet('dist', 'args')
const BOUNDS_KEYS = keySet('lower', 'upper')
const TERM_KEYS = keySet('data', 'coef')
const COMPONENT_KEYS = keySet('id', 'name', 'label', 'mean', 'scale')
const CORREL_KEYS = keySet('name', 'label', 'eta')
const NODE_KEYS: Record<string, Set<string>> = {
  data: keySet(...NODE_COMMON, 'type', 'support', 'target', 'standardize'),
  parameter: keySet(...NODE_COMMON, 'prior', 'bounds'),
  transformed: keySet(...NODE_COMMON, 'value'),
  linear_predictor: keySet(...NODE_COMMON, 'intercept', 'terms'),
  likelihood: keySet(...NODE_COMMON, 'observed', 'dist', 'args'),
  state: keySet(...NODE_COMMON, 'process', 'init', 'scale', 'form'),
  correlated_effects: keySet(...NODE_COMMON, 'components', 'correlation'),
}

type JObj = Map<string, JVal>
const isObj = (v: JVal | undefined): v is JObj => v instanceof Map

function exact (where: string, o: JObj, allowed: Set<string>): void {
  const bad = [...o.keys()].filter(k => !allowed.has(k)).sort(cmp)
  if (bad.length) throw new JsonFail(`${where}: unknown key ${JSON.stringify(bad[0])}`)
}

function objAt (p: string, v: JVal | undefined, allowed: Set<string>, more?: (q: string, o: JObj) => void): void {
  if (v === undefined || v === null) return
  if (!isObj(v)) throw new JsonFail(`${p}: must be an object`)
  exact(p, v, allowed)
  more?.(p, v)
}

const slotAt = (p: string, v: JVal | undefined) => objAt(p, v, SLOT_KEYS)

function argsAt (p: string, v: JVal | undefined): void {
  if (v === undefined || v === null) return
  if (!isObj(v)) throw new JsonFail(`${p}: must be an object`)
  for (const [k, s] of v) slotAt(`${p}.${k}`, s)
}

const distAt = (p: string, v: JVal | undefined) => objAt(p, v, DIST_KEYS, (q, o) => argsAt(`${q}.args`, o.get('args')))

function listAt (p: string, v: JVal | undefined, each: (q: string, o: JObj) => void): void {
  if (v === undefined || v === null) return
  if (!Array.isArray(v)) throw new JsonFail(`${p}: must be an array`)
  v.forEach((e, i) => {
    const q = `${p}[${i}]`
    if (!isObj(e)) throw new JsonFail(`${q}: must be an object`)
    each(q, e)
  })
}

function checkNode (p: string, e: JVal): void {
  if (!isObj(e)) throw new JsonFail(`${p}: must be an object`)
  const kindV = e.get('kind')
  const kind = typeof kindV === 'string' ? kindV : ''
  const allowed = Object.hasOwn(NODE_KEYS, kind) ? NODE_KEYS[kind] : undefined
  if (!allowed) throw new JsonFail(`${p}: unknown kind ${JSON.stringify(kindV ?? null)}`)
  exact(`${p} (${kind})`, e, allowed)
  for (const k of ['value', 'intercept', 'scale']) slotAt(`${p}.${k}`, e.get(k))
  for (const k of ['prior', 'init']) distAt(`${p}.${k}`, e.get(k))
  argsAt(`${p}.args`, e.get('args'))
  objAt(`${p}.bounds`, e.get('bounds'), BOUNDS_KEYS)
  listAt(`${p}.terms`, e.get('terms'), (q, t) => { exact(q, t, TERM_KEYS); slotAt(`${q}.coef`, t.get('coef')) })
  listAt(`${p}.components`, e.get('components'), (q, c) => {
    exact(q, c, COMPONENT_KEYS)
    for (const k of ['mean', 'scale']) slotAt(`${q}.${k}`, c.get(k))
  })
  objAt(`${p}.correlation`, e.get('correlation'), CORREL_KEYS, (q, c) => slotAt(`${q}.eta`, c.get('eta')))
}

/** Refuses any key the schema does not have for that object. */
function checkKeys (root: JObj): void {
  exact('the document', root, GRAPH_KEYS)
  const plates = root.get('plates')
  if (plates !== undefined && plates !== null) {
    if (!Array.isArray(plates)) throw new JsonFail('plates: must be an array')
    plates.forEach((e, i) => {
      const p = `plates[${i}]`
      if (!isObj(e)) throw new JsonFail(`${p}: must be an object`)
      const kindV = e.get('kind')
      const kind = typeof kindV === 'string' ? kindV : ''
      const allowed = Object.hasOwn(PLATE_KEYS, kind) ? PLATE_KEYS[kind] : undefined
      if (!allowed) throw new JsonFail(`${p}: unknown kind ${JSON.stringify(kindV ?? null)}`)
      exact(p, e, allowed)
      const s = e.get('series')
      if (isObj(s)) exact(`${p}.series`, s, SERIES_KEYS)
      else if (s !== undefined && s !== null) throw new JsonFail(`${p}.series: must be an object`)
    })
  }
  const nodes = root.get('nodes')
  if (nodes !== undefined && nodes !== null) {
    if (!Array.isArray(nodes)) throw new JsonFail('nodes: must be an array')
    nodes.forEach((e, i) => checkNode(`nodes[${i}]`, e))
  }
  const layout = root.get('layout')
  if (layout !== undefined && layout !== null) {
    if (!isObj(layout)) throw new JsonFail('layout: must be an object')
    for (const [id, b] of layout) {
      if (!isObj(b)) throw new JsonFail(`layout.${id}: must be an object`)
      exact(`layout.${id}`, b, BOX_KEYS)
    }
  }
}

// The typed reading (what Go's struct decoding refuses): a string where a string is, and so on.
const fail = (p: string, want: string): never => { throw new JsonFail(`${p}: must be ${want}`) }
const dStr = (p: string, v: JVal | undefined): string => (v === undefined || v === null ? '' : typeof v === 'string' ? v : fail(p, 'a string'))
const dOptStr = (p: string, v: JVal | undefined): string | null => (v === undefined || v === null ? null : typeof v === 'string' ? v : fail(p, 'a string'))
const dBool = (p: string, v: JVal | undefined): boolean => (v === undefined || v === null ? false : typeof v === 'boolean' ? v : fail(p, 'a boolean'))
function dOptNum (p: string, v: JVal | undefined): number | null {
  if (v === undefined || v === null) return null
  if (!(v instanceof JNum) || !Number.isFinite(v.num)) return fail(p, 'a number')
  return v.num
}
const dObj = (p: string, v: JVal | undefined): JObj | null => (v === undefined || v === null ? null : isObj(v) ? v : fail(p, 'an object'))
const dList = (p: string, v: JVal | undefined): JVal[] => (v === undefined || v === null ? [] : Array.isArray(v) ? v : fail(p, 'an array'))

function dSlot (p: string, v: JVal | undefined): BayesGraphSlot {
  const o = dObj(p, v)
  return { expr: dOptStr(`${p}.expr`, o?.get('expr')), ref: dOptStr(`${p}.ref`, o?.get('ref')) }
}
const dOptSlot = (p: string, v: JVal | undefined): BayesGraphSlot | null => (v === undefined || v === null ? null : dSlot(p, v))

function dArgs (p: string, v: JVal | undefined): Record<string, BayesGraphSlot> {
  const out: Record<string, BayesGraphSlot> = Object.create(null)
  const o = dObj(p, v)
  if (o) for (const [k, s] of o) out[k] = dSlot(`${p}.${k}`, s)
  return out
}

function dDist (p: string, v: JVal | undefined): BayesGraphDist | null {
  const o = dObj(p, v)
  if (!o) return null
  return { dist: dStr(`${p}.dist`, o.get('dist')), args: dArgs(`${p}.args`, o.get('args')) }
}

function decodeGraph (root: JObj): BayesGraph {
  const plates = dList('plates', root.get('plates')).map((e, i): BayesGraphPlate => {
    const p = `plates[${i}]`
    const o = e as JObj
    const s = dObj(`${p}.series`, o.get('series'))
    return {
      id: dStr(`${p}.id`, o.get('id')),
      kind: dStr(`${p}.kind`, o.get('kind')),
      label: dStr(`${p}.label`, o.get('label')),
      note: dStr(`${p}.note`, o.get('note')),
      series: s ? { time: dStr(`${p}.series.time`, s.get('time')), by: dOptStr(`${p}.series.by`, s.get('by')) } : null,
      parent: dOptStr(`${p}.parent`, o.get('parent')),
      key: dStr(`${p}.key`, o.get('key')),
      form: dStr(`${p}.form`, o.get('form')),
    }
  })
  const nodes = dList('nodes', root.get('nodes')).map((e, i): BayesGraphNode => {
    const p = `nodes[${i}]`
    const o = e as JObj
    const b = dObj(`${p}.bounds`, o.get('bounds'))
    const cor = dObj(`${p}.correlation`, o.get('correlation'))
    return {
      id: dStr(`${p}.id`, o.get('id')),
      kind: dStr(`${p}.kind`, o.get('kind')),
      name: dOptStr(`${p}.name`, o.get('name')),
      label: dStr(`${p}.label`, o.get('label')),
      plate: dOptStr(`${p}.plate`, o.get('plate')),
      note: dStr(`${p}.note`, o.get('note')),
      type: dStr(`${p}.type`, o.get('type')),
      support: dStr(`${p}.support`, o.get('support')),
      target: dStr(`${p}.target`, o.get('target')),
      standardize: dBool(`${p}.standardize`, o.get('standardize')),
      prior: dDist(`${p}.prior`, o.get('prior')),
      bounds: b ? { lower: dOptNum(`${p}.bounds.lower`, b.get('lower')), upper: dOptNum(`${p}.bounds.upper`, b.get('upper')) } : null,
      value: dOptSlot(`${p}.value`, o.get('value')),
      intercept: dOptSlot(`${p}.intercept`, o.get('intercept')),
      terms: dList(`${p}.terms`, o.get('terms')).map((t, j) => ({
        data: dStr(`${p}.terms[${j}].data`, (t as JObj).get('data')),
        coef: dSlot(`${p}.terms[${j}].coef`, (t as JObj).get('coef')),
      })),
      observed: dStr(`${p}.observed`, o.get('observed')),
      dist: dStr(`${p}.dist`, o.get('dist')),
      args: dArgs(`${p}.args`, o.get('args')),
      process: dStr(`${p}.process`, o.get('process')),
      init: dDist(`${p}.init`, o.get('init')),
      scale: dOptSlot(`${p}.scale`, o.get('scale')),
      form: dStr(`${p}.form`, o.get('form')),
      components: dList(`${p}.components`, o.get('components')).map((c, j) => {
        const q = `${p}.components[${j}]`
        const co = c as JObj
        return {
          id: dStr(`${q}.id`, co.get('id')),
          name: dStr(`${q}.name`, co.get('name')),
          label: dStr(`${q}.label`, co.get('label')),
          mean: dSlot(`${q}.mean`, co.get('mean')),
          scale: dSlot(`${q}.scale`, co.get('scale')),
        }
      }),
      correlation: cor
        ? { name: dStr(`${p}.correlation.name`, cor.get('name')), label: dStr(`${p}.correlation.label`, cor.get('label')), eta: dSlot(`${p}.correlation.eta`, cor.get('eta')) }
        : null,
    }
  })
  // The layout is the canvas's alone: read for its types, not kept.
  const layout = dObj('layout', root.get('layout'))
  if (layout) {
    for (const [id, b] of layout) {
      const o = b as JObj
      dOptNum(`layout.${id}.x`, o.get('x'))
      dOptNum(`layout.${id}.y`, o.get('y'))
      dOptNum(`layout.${id}.w`, o.get('w'))
      dOptNum(`layout.${id}.h`, o.get('h'))
    }
  }
  return { formatVersion: BAYES_GRAPH_FORMAT_VERSION, plates, nodes }
}

/**
 * Reads a graph strictly, as the gateway does: the document within max_graph_bytes, well-formed
 * JSON without a duplicate key, a known format_version, no unknown key, the right JSON types, at
 * most max_nodes nodes and max_plates plates. The graph, or null and the findings (graph.format,
 * graph.too_big). It does not check the rules of the model.
 */
export function bayesGraphDecode (text: string): { graph: BayesGraph | null, findings: BayesGraphFinding[] } {
  const none = (...fs: BayesGraphFinding[]) => ({ graph: null, findings: fs })
  const maxBytes = bayesLimit('max_graph_bytes')
  const bytes = utf8Bytes(text)
  if (bytes > maxBytes) return none(tooBig('bytes', bytes, maxBytes))
  let tree: JVal
  try { tree = parseTree(text) } catch (e) {
    if (e instanceof JsonFail) return none(formatFinding(e.message))
    throw e
  }
  if (!isObj(tree)) return none(formatFinding('the document must be a JSON object'))
  const ver = tree.get('format_version')
  if (ver === undefined) return none(formatFinding('format_version is missing'))
  if (!(ver instanceof JNum) || !/^-?[0-9]+$/.test(ver.raw)) return none(formatFinding('format_version must be an integer'))
  if (ver.num < 1 || ver.num > 2 ** 31) return none(formatFinding('format_version must be an integer of at least 1'))
  if (ver.num > BAYES_GRAPH_FORMAT_VERSION) {
    return none(formatFinding(`format_version ${ver.num} is newer than the ${BAYES_GRAPH_FORMAT_VERSION} this gateway reads`, { version: ver.num }))
  }
  try {
    checkKeys(tree)
    const n = Array.isArray(tree.get('nodes')) ? (tree.get('nodes') as JVal[]).length : 0
    if (n > bayesLimit('max_nodes')) return none(tooBig('nodes', n, bayesLimit('max_nodes')))
    const pl = Array.isArray(tree.get('plates')) ? (tree.get('plates') as JVal[]).length : 0
    if (pl > bayesLimit('max_plates')) return none(tooBig('plates', pl, bayesLimit('max_plates')))
    return { graph: decodeGraph(tree), findings: [] }
  } catch (e) {
    if (e instanceof JsonFail) return none(formatFinding(e.message))
    throw e
  }
}

// ---------------------------------------------------------------- structure

const validId = (s: string): boolean => /^[a-z][a-z0-9]{0,15}$/.test(s)
const validName = (s: string): boolean => /^[a-z][a-z0-9_]*$/.test(s)

function forbiddenRune (cp: number, note: boolean): string {
  if (note && (cp === 10 || cp === 9)) return ''
  if (cp <= 0x1f || (cp >= 0x7f && cp <= 0x9f)) return 'control'
  if ((cp >= 0x202a && cp <= 0x202e) || (cp >= 0x2066 && cp <= 0x2069)) return 'direction'
  if (cp === 0xfeff) return 'bom'
  return ''
}

function firstForbidden (s: string, note: boolean): string {
  for (const ch of s) {
    const why = forbiddenRune(ch.codePointAt(0) as number, note)
    if (why) return why
  }
  return ''
}

/**
 * The rules that need no expression and no model: graph.too_big (nodes and plates), graph.id,
 * graph.name, graph.name_taken, graph.name_function, graph.label, graph.note, graph.rows,
 * graph.plate_parent, graph.plate_key and graph.series.
 */
export function bayesCheckStructure (g: BayesGraph, catalog: BayesCatalog = BAYES_CATALOG): BayesGraphFinding[] {
  const out: BayesGraphFinding[] = []
  const lim = (k: string) => bayesLimit(k, catalog)
  const add = (code: string, node: string, slot: string, args?: Record<string, unknown>) => { out.push(finding(code, 'error', node, slot, args)) }
  const functions = new Set(catalog.language.functions.map(f => f.name))

  if (g.nodes.length > lim('max_nodes')) out.push(tooBig('nodes', g.nodes.length, lim('max_nodes')))
  if (g.plates.length > lim('max_plates')) out.push(tooBig('plates', g.plates.length, lim('max_plates')))

  // ids
  const seen = new Map<string, number>()
  const bump = (id: string) => seen.set(id, (seen.get(id) ?? 0) + 1)
  for (const p of g.plates) {
    bump(p.id)
    if (!validId(p.id)) add('graph.id', p.id, '', { id: p.id, reason: 'malformed', what: 'plate' })
    if (p.kind === 'rows' && p.id !== 'rows') add('graph.id', p.id, '', { id: p.id, reason: 'rows_plate_id' })
    if (p.kind !== 'rows' && p.id === 'rows') add('graph.id', p.id, '', { id: p.id, reason: 'rows_reserved' })
  }
  for (const n of g.nodes) {
    bump(n.id)
    if (!validId(n.id)) add('graph.id', n.id, '', { id: n.id, reason: 'malformed', what: 'node' })
    if (n.id === 'rows') add('graph.id', n.id, '', { id: n.id, reason: 'rows_reserved' })
    const cs = new Map<string, number>()
    for (const k of n.components) {
      cs.set(k.id, (cs.get(k.id) ?? 0) + 1)
      if (!validId(k.id)) add('graph.id', `${n.id}.${k.id}`, '', { id: k.id, reason: 'malformed' })
    }
    for (const [id, k] of cs) if (k > 1) add('graph.id', `${n.id}.${id}`, '', { id, reason: 'duplicate' })
  }
  for (const [id, k] of seen) if (k > 1) add('graph.id', id, '', { id, reason: 'duplicate', count: k })

  // names
  const checkName = (node: string, name: string) => {
    if (name === '') add('graph.name', node, '', { name, reason: 'empty' })
    else if (!validName(name)) add('graph.name', node, '', { name, reason: 'pattern' })
    else if (name.length > lim('max_name_bytes')) add('graph.name', node, '', { name, reason: 'too_long', max: lim('max_name_bytes') })
    else if (name.endsWith('__')) add('graph.name', node, '', { name, reason: 'double_underscore' })
    else if (functions.has(name)) add('graph.name_function', node, '', { name })
  }
  const holders = new Map<string, string[]>()
  const hold = (name: string, node: string) => { const l = holders.get(name) ?? []; l.push(node); holders.set(name, l) }
  for (const n of g.nodes) {
    if (n.name !== null) { hold(n.name, n.id); checkName(n.id, n.name) }
    for (const k of n.components) {
      if (k.name !== '') { hold(k.name, `${n.id}.${k.id}`); checkName(`${n.id}.${k.id}`, k.name) }
    }
    if (n.correlation && n.correlation.name !== '') {
      hold(n.correlation.name, `${n.id}.correlation`)
      checkName(`${n.id}.correlation`, n.correlation.name)
    }
  }
  for (const [name, hs] of holders) {
    if (hs.length < 2) continue
    for (const h of hs) add('graph.name_taken', h, '', { name, count: hs.length })
  }

  // labels and notes
  const label = (node: string, s: string) => {
    const n = runeCount(s)
    if (n === 0) { add('graph.label', node, 'label', { reason: 'empty' }); return }
    if (n > lim('max_label_chars')) add('graph.label', node, 'label', { reason: 'too_long', max: lim('max_label_chars'), got: n })
    const why = firstForbidden(s, false)
    if (why) add('graph.label', node, 'label', { reason: why })
  }
  const note = (node: string, s: string) => {
    if (s === '') return
    const n = runeCount(s)
    if (n > lim('max_note_chars')) add('graph.note', node, 'note', { reason: 'too_long', max: lim('max_note_chars'), got: n })
    const why = firstForbidden(s, true)
    if (why) add('graph.note', node, 'note', { reason: why })
  }
  for (const p of g.plates) { label(p.id, p.label); note(p.id, p.note) }
  for (const n of g.nodes) {
    label(n.id, n.label)
    note(n.id, n.note)
    for (const k of n.components) label(`${n.id}.${k.id}`, k.label)
    if (n.correlation) label(`${n.id}.correlation`, n.correlation.label)
  }

  // plates
  const byID = new Map<string, BayesGraphPlate>()
  let rows = 0
  for (const p of g.plates) {
    if (!byID.has(p.id)) byID.set(p.id, p)
    if (p.kind === 'rows') rows++
  }
  if (rows === 0) add('graph.rows', 'rows', '', { count: 0 })
  else if (rows > 1) for (const p of g.plates) if (p.kind === 'rows') add('graph.rows', p.id, '', { count: rows })
  const nodes = new Map<string, BayesGraphNode>()
  for (const n of g.nodes) if (!nodes.has(n.id)) nodes.set(n.id, n)
  const onRows = (n: BayesGraphNode) => n.plate === 'rows'
  for (const p of g.plates) {
    if (p.kind === 'rows') {
      if (!p.series) continue
      const t = nodes.get(p.series.time)
      if (!t || t.kind !== 'data' || t.type !== 'time' || !onRows(t)) add('graph.series', p.id, 'series.time', { time: p.series.time })
      if (p.series.by !== null) {
        const b = byID.get(p.series.by)
        if (!b || b.kind !== 'group') add('graph.series', p.id, 'series.by', { by: p.series.by })
      }
    } else if (p.kind === 'group') {
      // parent
      if (p.parent !== null) {
        const par = byID.get(p.parent)
        if (!par || par.kind !== 'group') add('graph.plate_parent', p.id, 'parent', { parent: p.parent, reason: 'not_a_group_plate' })
        else {
          let depth = 1
          const visited = new Set<string>([p.id])
          let cur: BayesGraphPlate = p
          let stop = false
          while (cur.parent !== null) {
            const next = byID.get(cur.parent)
            if (!next || next.kind !== 'group') { stop = true; break }
            if (visited.has(next.id)) { add('graph.plate_parent', p.id, 'parent', { parent: p.parent, reason: 'cycle' }); stop = true; break }
            visited.add(next.id)
            depth++
            cur = next
          }
          if (!stop && depth > lim('max_plate_depth')) add('graph.plate_parent', p.id, 'parent', { reason: 'too_deep', depth, max: lim('max_plate_depth') })
        }
      }
      if (p.form !== 'non_centred' && p.form !== 'centred' && p.form !== '') {
        add('graph.format', p.id, 'form', { detail: `form ${JSON.stringify(p.form)} is not non_centred or centred` })
      }
      // key
      const targeting = g.nodes.filter(n => n.kind === 'data' && n.type === 'group_index' && n.target === p.id).map(n => n.id).sort(cmp)
      const k = nodes.get(p.key)
      if (p.key === '') add('graph.plate_key', p.id, 'key', { reason: 'missing' })
      else if (!k) add('graph.plate_key', p.id, 'key', { key: p.key, reason: 'unknown_node' })
      else if (k.kind !== 'data' || k.type !== 'group_index') add('graph.plate_key', p.id, 'key', { key: p.key, reason: 'not_a_group_index' })
      else if (k.target !== p.id) add('graph.plate_key', p.id, 'key', { key: p.key, reason: 'other_target' })
      else if (!onRows(k)) add('graph.plate_key', p.id, 'key', { key: p.key, reason: 'not_on_rows' })
      if (targeting.length > 1) add('graph.plate_key', p.id, 'key', { reason: 'several_keys', nodes: targeting.join(',') })
    } else {
      add('graph.format', p.id, 'kind', { detail: `plate kind ${JSON.stringify(p.kind)} is unknown` })
    }
  }
  for (const n of g.nodes) {
    if (n.kind !== 'data' || n.type !== 'group_index') continue
    const t = byID.get(n.target)
    if (!t || t.kind !== 'group') add('graph.plate_key', n.id, 'target', { target: n.target, reason: 'not_a_group_plate' })
  }
  bayesSortFindings(out)
  return out
}

// ---------------------------------------------------------------- the environment of a slot

const LEVELS_PREFIX = 'levels:'

/** The id of the implicit levels plate of a category data node. */
export const bayesLevelsPlate = (category: string): string => LEVELS_PREFIX + category

interface EnvPlate { kind: string, parent: string, series: boolean, index: string }

const plateOf = (n: BayesGraphNode): string => n.plate ?? ''

const dataInterval = (n: BayesGraphNode, catalog: BayesCatalog): BayesExprInterval | null => {
  if (n.type !== 'int') return null
  if (n.support === 'nonnegative') return bayesNamedInterval('non_negative', catalog)
  if (n.support === 'binary') return { lo: 0, hi: 1, loOpen: false, hiOpen: false }
  return null
}

class GraphEnv {
  plates = new Map<string, EnvPlate>()
  byName = new Map<string, BayesExprSymbol>()
  holder = new Map<string, string>()
  names = new Map<string, string>()
  nodes = new Map<string, BayesGraphNode>()
  components = new Map<string, BayesGraphComponent>()
  compOwner = new Map<string, BayesGraphNode>()
  ranges: Map<string, BayesExprInterval>
  catalog: BayesCatalog

  constructor (g: BayesGraph, ranges: Map<string, BayesExprInterval>, catalog: BayesCatalog) {
    this.ranges = ranges
    this.catalog = catalog
    for (const p of g.plates) {
      if (this.plates.has(p.id)) continue
      const ep: EnvPlate = { kind: p.kind, parent: '', series: false, index: '' }
      if (p.kind === 'rows') ep.series = p.series !== null
      else if (p.kind === 'group') { ep.parent = p.parent ?? ''; ep.index = p.key }
      this.plates.set(p.id, ep)
    }
    for (const n of g.nodes) {
      if (this.nodes.has(n.id)) continue
      this.nodes.set(n.id, n)
      if (n.kind === 'data' && n.type === 'category') this.plates.set(bayesLevelsPlate(n.id), { kind: 'levels', parent: '', series: false, index: n.id })
      for (const k of n.components) {
        const id = `${n.id}.${k.id}`
        if (!this.components.has(id)) { this.components.set(id, k); this.compOwner.set(id, n) }
      }
    }
    for (const n of g.nodes) {
      if (this.nodes.get(n.id) !== n) continue
      if (n.name !== null && n.name !== '') this.add(n.name, this.nodeSymbol(n))
      const plate = plateOf(n)
      for (const k of n.components) {
        if (k.name !== '') this.add(k.name, { id: `${n.id}.${k.id}`, role: 'component', type: { elem: 'real', plate } })
      }
      if (n.correlation && n.correlation.name !== '') {
        this.add(n.correlation.name, { id: `${n.id}.correlation`, role: 'correlation', type: { elem: 'not_number', plate: '' } })
      }
    }
  }

  private add (name: string, s: BayesExprSymbol): void {
    this.names.set(s.id, name)
    const h = this.holder.get(name)
    if (h !== undefined && h < s.id) return
    this.holder.set(name, s.id)
    this.byName.set(name, s)
  }

  nodeSymbol (n: BayesGraphNode): BayesExprSymbol {
    const s: BayesExprSymbol = { id: n.id, role: n.kind as BayesExprSymbol['role'], type: { elem: 'real', plate: plateOf(n) } }
    switch (n.kind) {
      case 'data': {
        s.role = 'data'
        if (n.type === 'int') s.type.elem = 'int'
        else if (n.type === 'group_index') s.type = { elem: 'index', plate: plateOf(n), target: n.target }
        else if (n.type === 'category') s.type = { elem: 'index', plate: plateOf(n), target: bayesLevelsPlate(n.id) }
        else if (n.type === 'time') s.type.elem = 'time'
        s.interval = dataInterval(n, this.catalog)
        break
      }
      case 'parameter': {
        const r = this.ranges.get(n.id)
        if (r) s.interval = r
        break
      }
      case 'transformed': case 'linear_predictor': case 'state': break
      default: s.type = { elem: 'not_number', plate: '' }
    }
    return s
  }

  lookupID (id: string): BayesExprSymbol | undefined {
    const n = this.nodes.get(id)
    if (n) return this.nodeSymbol(n)
    if (this.components.has(id)) {
      return { id, role: 'component', type: { elem: 'real', plate: plateOf(this.compOwner.get(id) as BayesGraphNode) } }
    }
    if (id.endsWith('.correlation')) {
      const owner = this.nodes.get(id.slice(0, -'.correlation'.length))
      if (owner && owner.correlation) return { id, role: 'correlation', type: { elem: 'not_number', plate: '' } }
    }
    return undefined
  }

  encloses (outer: string, inner: string): boolean {
    if (outer === inner) return true
    const o = this.plates.get(outer)
    const inn = this.plates.get(inner)
    if (!o || !inn) return false
    if (o.kind === 'group') {
      if (inn.kind === 'rows') return true
      if (inn.kind !== 'group') return false
      const seen = new Set<string>([inner])
      let cur = inn
      while (cur.parent !== '') {
        const p = cur.parent
        if (p === outer) return true
        if (seen.has(p)) return false
        seen.add(p)
        const next = this.plates.get(p)
        if (!next || next.kind !== 'group') return false
        cur = next
      }
      return false
    }
    if (o.kind === 'levels') return inn.kind === 'rows'
    return false
  }

  slotEnv (ctx: string): BayesExprEnv {
    return {
      lookup: name => this.byName.get(name),
      context: () => ctx,
      encloses: (outer, inner) => this.encloses(outer, inner),
      isSeries: p => { const ep = this.plates.get(p); return Boolean(ep && ep.kind === 'rows' && ep.series) },
      indexOf: target => {
        const ep = this.plates.get(target)
        if (!ep || ep.index === '') return undefined
        return this.names.get(ep.index)
      },
    }
  }
}

// ---------------------------------------------------------------- intervals of ranges

const intersect = (a: BayesExprInterval, b: BayesExprInterval): BayesExprInterval => {
  const r = { ...a }
  if (b.lo > r.lo || (b.lo === r.lo && b.loOpen)) { r.lo = b.lo; r.loOpen = b.loOpen || (b.lo === a.lo && a.loOpen) }
  if (b.hi < r.hi || (b.hi === r.hi && b.hiOpen)) { r.hi = b.hi; r.hiOpen = b.hiOpen || (b.hi === a.hi && a.hiOpen) }
  return r
}
const emptyInterval = (i: BayesExprInterval): boolean => i.lo > i.hi || (i.lo === i.hi && (i.loOpen || i.hiOpen))
const sameInterval = (a: BayesExprInterval, b: BayesExprInterval): boolean =>
  a.lo === b.lo && a.hi === b.hi && a.loOpen === b.loOpen && a.hiOpen === b.hiOpen

function boundsInterval (b: BayesGraphBounds | null): BayesExprInterval {
  const i = bayesUnbounded()
  if (!b) return i
  if (b.lower !== null) { i.lo = b.lower; i.loOpen = true }
  if (b.upper !== null) { i.hi = b.upper; i.hiOpen = true }
  return i
}

/** The range a support and bounds make (the support ∩ the bounds, open ends), whether it is empty, whether it is narrower than the support. */
export function bayesTruncationRange (support: BayesExprInterval, b: BayesGraphBounds | null): { range: BayesExprInterval, empty: boolean, truncated: boolean } {
  const range = intersect(support, boundsInterval(b))
  if (emptyInterval(range)) return { range, empty: true, truncated: false }
  return { range, empty: false, truncated: !sameInterval(range, support) }
}

const canTruncate = (d: BayesDistribution): boolean => Boolean(d.stan.lcdf && d.stan.lccdf)

// ---------------------------------------------------------------- the validator

/** A slot as the validator finds it. */
interface SlotSpec {
  node: string
  slot: string
  owner: BayesGraphNode
  value: BayesGraphSlot | null
  ctx: string
  scalar: boolean
  need: string
  constant: boolean
  prior: boolean
  tree: BayesExprNode | null
  ok: boolean
}

/** A slot that typed. */
export interface BayesTypedSlot {
  context: string
  tree: BayesExprTyped | null
  ref: string
  type: BayesExprType
  interval: BayesExprInterval
  constant: number | null
  refs: string[]
  indices: string[]
  lags: number[]
  lifts: BayesExprLift[]
}

export interface BayesParam {
  dist: string
  support: BayesExprInterval
  range: BayesExprInterval
  truncated: boolean
  truncateInProgram: boolean
  role: 'prior' | 'hyperprior'
  nonCentred: boolean
}

export interface BayesEdge { from: string, to: string, node: string, slot: string, kind: 'read' | 'index' | 'lag' | 'term' | 'observed' }

const slotKey = (node: string, slot: string) => `${node}\u0001${slot}`
const ownerOf = (id: string): string => { const i = id.indexOf('.'); return i >= 0 ? id.slice(0, i) : id }

const constraintNeed = (c: string): string => (c === 'positive' ? 'positive' : c === 'non_negative_integer' ? 'non_negative' : '')

function constantValue (n: BayesExprNode | null): number | null {
  let sign = 1
  while (n && n.kind === 'neg') { sign = -sign; n = n.args[0] }
  if (!n || n.kind !== 'num') return null
  return sign * n.num
}

function countNodes (n: BayesExprNode | null): number {
  if (!n) return 0
  let c = 1
  if ('args' in n) for (const a of n.args as BayesExprNode[]) c += countNodes(a)
  return c
}

interface ReadSets { plain: string[], lag: string[], index: string[] }

interface Deps {
  edges: BayesEdge[]
  reads: Map<string, string[]>
  plain: Map<string, string[]>
}

class Validation {
  g: BayesGraph
  catalog: BayesCatalog
  env!: GraphEnv
  specs: SlotSpec[] = []
  slots = new Map<string, BayesTypedSlot>()
  params = new Map<string, BayesParam>()
  deps!: Deps
  out: BayesGraphFinding[] = []

  constructor (g: BayesGraph, catalog: BayesCatalog) {
    this.g = g
    this.catalog = catalog
  }

  lim (k: string): number { return bayesLimit(k, this.catalog) }
  dist (id: string): BayesDistribution | undefined { return this.catalog.distributions.find(d => d.id === id) }
  param (d: BayesDistribution): Array<{ name: string, constraint: string, constant?: boolean, data?: boolean }> { return d.params as any }

  add (sev: BayesGraphSeverity, code: string, node: string, slot: string, args?: Record<string, unknown>): void {
    this.out.push(finding(code, sev, node, slot, args))
  }

  error (code: string, node: string, slot: string, args?: Record<string, unknown>): void { this.add('error', code, node, slot, args) }

  nodeList (): BayesGraphNode[] { return [...this.env.nodes.values()].sort((a, b) => cmp(a.id, b.id)) }
  sortedIDs (): string[] { return sortedStrings(this.env.nodes.keys()) }

  run (): this {
    this.out = bayesCheckStructure(this.g, this.catalog)
    if (this.out.some(f => f.code === 'graph.id' || f.code === 'graph.rows')) return this
    this.env = new GraphEnv(this.g, new Map(), this.catalog)
    for (const n of this.nodeList()) {
      this.nodePlate(n)
      if (n.kind === 'data') { this.dataType(n); this.standardize(n) }
    }
    this.collectSlots()
    const ranges = new Map<string, BayesExprInterval>()
    for (const n of this.nodeList()) {
      if (n.kind !== 'parameter') continue
      const p = this.parameter(n)
      if (p) { this.params.set(n.id, p); ranges.set(n.id, p.range) }
    }
    this.env = new GraphEnv(this.g, ranges, this.catalog)
    this.typeSlots()
    this.priorData()
    this.modelRules()
    bayesSortFindings(this.out)
    return this
  }

  // ---- nodes, data, priors, bounds

  nodePlate (n: BayesGraphNode): void {
    const p = plateOf(n)
    let kind = ''
    if (p !== '') {
      const ep = this.env.plates.get(p)
      if (!ep) { this.error('graph.node_plate', n.id, 'plate', { reason: 'unknown_plate', plate: p }); return }
      kind = ep.kind
    }
    if (n.kind === 'likelihood') {
      const obs = this.env.nodes.get(n.observed)
      if (obs && obs.kind === 'data' && p !== '' && p !== plateOf(obs)) {
        this.error('graph.node_plate', n.id, 'plate', { reason: 'not_observed_plate', plate: p, observed: n.observed })
      }
      return
    }
    const table: Record<string, string[]> = {
      data: ['rows', 'group'],
      parameter: ['', 'group', 'levels'],
      transformed: ['', 'rows', 'group'],
      linear_predictor: ['rows', 'group'],
      state: ['rows'],
      correlated_effects: ['group'],
    }
    const allowed = table[n.kind]
    if (!allowed || allowed.includes(kind)) return
    const args: Record<string, unknown> = { reason: 'not_allowed', kind: n.kind }
    if (p !== '') args.plate = p
    this.error('graph.node_plate', n.id, 'plate', args)
  }

  dataType (n: BayesGraphNode): void {
    if (!['real', 'int', 'group_index', 'category', 'time'].includes(n.type)) {
      this.error('graph.data_type', n.id, 'type', { reason: 'unknown_type', type: n.type })
    }
    if (n.type === 'int' && n.support === '') this.error('graph.data_type', n.id, 'support', { reason: 'missing_support' })
    else if (n.type === 'int' && !['any', 'nonnegative', 'binary'].includes(n.support)) {
      this.error('graph.data_type', n.id, 'support', { reason: 'unknown_support', support: n.support })
    } else if (n.type !== 'int' && n.support !== '') {
      this.error('graph.data_type', n.id, 'support', { reason: 'support_not_int', support: n.support })
    }
    if (n.target !== '' && n.type !== 'group_index') this.error('graph.data_type', n.id, 'target', { reason: 'target_not_group_index' })
    if (['group_index', 'category', 'time'].includes(n.type)) {
      const ep = this.env.plates.get(plateOf(n))
      if (ep && ep.kind === 'group') this.error('graph.data_type', n.id, 'plate', { reason: 'not_on_rows', type: n.type, plate: plateOf(n) })
    }
  }

  standardize (n: BayesGraphNode): void {
    if (!n.standardize) return
    if (n.type !== 'real') { this.error('graph.standardize', n.id, 'standardize', { reason: 'not_real', type: n.type }); return }
    const by = this.nodeList().find(l => l.kind === 'likelihood' && l.observed === n.id)
    if (by) this.error('graph.standardize', n.id, 'standardize', { reason: 'observed', likelihood: by.id })
  }

  /** A prior's support as an interval; null when it cannot be told (a uniform whose bounds are not constants or not ordered). */
  supportInterval (d: BayesDistribution, consts: Map<string, number>): BayesExprInterval | null {
    switch (d.support) {
      case 'real': case 'positive': case 'unit': return bayesNamedInterval(d.support, this.catalog)
      case 'bounded': {
        const ps = this.param(d)
        if (ps.length !== 2) return null
        const a = consts.get(ps[0].name)
        const b = consts.get(ps[1].name)
        if (a === undefined || b === undefined || !(a < b)) return null
        return { lo: a, hi: b, loOpen: true, hiOpen: true }
      }
    }
    return null
  }

  parameter (n: BayesGraphNode): BayesParam | null {
    const ok = this.prior(n)
    const bok = this.boundsShape(n)
    if (!n.prior) return null
    const d = this.dist(n.prior.dist)
    if (!d || !d.uses.includes('prior') || d.support === 'cholesky_factor_corr') return null
    const consts = new Map<string, number>()
    let allConst = true
    for (const p of this.param(d)) {
      const c = this.constantOf(slotKey(n.id, 'prior.' + p.name))
      if (c !== null) consts.set(p.name, c)
      else allConst = false
    }
    const support = this.supportInterval(d, consts)
    if (!support) { this.uniformOrder(n, d, consts); return null }
    if (!bok) return null
    const { range, empty, truncated } = bayesTruncationRange(support, n.bounds)
    let reason = ''
    if (empty) reason = 'empty'
    else if (truncated && !canTruncate(d)) reason = 'not_truncatable'
    if (reason) { this.error('graph.bounds', n.id, 'bounds', { reason, dist: d.id }); return null }
    if (!ok) return null
    return { dist: d.id, support, range, truncated, truncateInProgram: truncated && !allConst, role: 'prior', nonCentred: false }
  }

  uniformOrder (n: BayesGraphNode, d: BayesDistribution, consts: Map<string, number>): void {
    const ps = this.param(d)
    if (d.support !== 'bounded' || ps.length !== 2) return
    const a = consts.get(ps[0].name)
    const b = consts.get(ps[1].name)
    if (a !== undefined && b !== undefined && !(a < b)) {
      this.error('graph.slot_interval', n.id, 'prior.' + ps[1].name, { need: 'above_' + ps[0].name })
    }
  }

  prior (n: BayesGraphNode): boolean {
    if (!n.prior) { this.error('graph.prior', n.id, 'prior', { reason: 'missing' }); return false }
    const d = this.dist(n.prior.dist)
    if (!d) { this.error('graph.prior', n.id, 'prior', { reason: 'unknown_dist', dist: n.prior.dist }); return false }
    if (d.support === 'cholesky_factor_corr') { this.error('graph.prior', n.id, 'prior', { reason: 'effects_only', dist: d.id }); return false }
    if (!d.uses.includes('prior')) { this.error('graph.prior', n.id, 'prior', { reason: 'not_prior', dist: d.id }); return false }
    let ok = true
    const want = new Set<string>()
    for (const p of this.param(d)) {
      want.add(p.name)
      if (!(p.name in n.prior.args)) {
        this.error('graph.prior', n.id, 'prior.' + p.name, { reason: 'missing_arg', dist: d.id, arg: p.name })
        ok = false
      }
    }
    for (const arg of Object.keys(n.prior.args)) {
      if (!want.has(arg)) {
        this.error('graph.prior', n.id, 'prior.' + arg, { reason: 'unknown_arg', dist: d.id, arg })
        ok = false
      }
    }
    return ok
  }

  boundsShape (n: BayesGraphNode): boolean {
    const b = n.bounds
    if (!b) return true
    let ok = true
    const max = this.lim('max_abs_literal')
    const one = (slot: string, x: number | null) => {
      if (x === null) return
      if (!Number.isFinite(x)) { this.error('graph.bounds', n.id, slot, { reason: 'not_finite' }); ok = false } else if (Math.abs(x) > max) {
        this.error('graph.bounds', n.id, slot, { reason: 'too_large', max })
        ok = false
      }
    }
    one('bounds.lower', b.lower)
    one('bounds.upper', b.upper)
    if (ok && b.lower !== null && b.upper !== null && !(b.lower < b.upper)) {
      this.error('graph.bounds', n.id, 'bounds', { reason: 'order' })
      ok = false
    }
    return ok
  }

  // ---- slots

  collectSlots (): void {
    for (const n of this.nodeList()) {
      const ctx = plateOf(n)
      switch (n.kind) {
        case 'parameter':
          if (n.prior) this.distSlots(n, 'prior.', n.prior.dist, n.prior.args, ctx, true)
          break
        case 'transformed':
          this.spec({ node: n.id, slot: 'value', owner: n, value: n.value, ctx })
          break
        case 'linear_predictor':
          if (n.intercept) this.spec({ node: n.id, slot: 'intercept', owner: n, value: n.intercept, ctx })
          n.terms.forEach((t, i) => {
            let tctx = ctx
            const d = this.env.nodes.get(t.data)
            if (d && d.kind === 'data' && d.type === 'category') tctx = bayesLevelsPlate(d.id)
            this.spec({ node: n.id, slot: `terms[${i}].coef`, owner: n, value: t.coef, ctx: tctx })
          })
          break
        case 'likelihood': {
          let c = ctx
          const obs = this.env.nodes.get(n.observed)
          if (obs && obs.kind === 'data') c = plateOf(obs)
          this.distSlots(n, 'args.', n.dist, n.args, c, false)
          break
        }
        case 'state':
          if (n.init) this.distSlots(n, 'init.', n.init.dist, n.init.args, ctx, true)
          this.spec({ node: n.id, slot: 'scale', owner: n, value: n.scale, ctx, scalar: true, need: 'positive', prior: true })
          break
        case 'correlated_effects':
          for (const k of n.components) {
            const id = `${n.id}.${k.id}`
            this.spec({ node: id, slot: 'mean', owner: n, value: k.mean, ctx, scalar: true, prior: true })
            this.spec({ node: id, slot: 'scale', owner: n, value: k.scale, ctx, scalar: true, need: 'positive', prior: true })
          }
          if (n.correlation) {
            const s: Partial<SlotSpec> = { node: `${n.id}.correlation`, slot: 'eta', owner: n, value: n.correlation.eta, ctx, scalar: true }
            const d = this.dist('lkj_corr_cholesky')
            if (d) for (const p of this.param(d)) if (p.name === 'eta') { s.need = constraintNeed(p.constraint); s.constant = Boolean(p.constant) }
            this.spec(s)
          }
          break
      }
    }
    let total = 0
    for (const s of this.specs) total += countNodes(s.tree)
    if (total > this.lim('max_total_expression_nodes')) this.out.push(tooBig('expression_nodes', total, this.lim('max_total_expression_nodes')))
  }

  distSlots (n: BayesGraphNode, prefix: string, dist: string, args: Record<string, BayesGraphSlot>, ctx: string, prior: boolean): void {
    const d = this.dist(dist)
    for (const a of Object.keys(args).sort(cmp)) {
      const s: Partial<SlotSpec> = { node: n.id, slot: prefix + a, owner: n, value: args[a], ctx, prior }
      if (d) for (const p of this.param(d)) if (p.name === a) { s.need = constraintNeed(p.constraint); s.constant = Boolean(p.constant) }
      this.spec(s)
    }
  }

  spec (p: Partial<SlotSpec>): void {
    const s: SlotSpec = {
      node: p.node as string, slot: p.slot as string, owner: p.owner as BayesGraphNode, value: p.value ?? null, ctx: p.ctx ?? '',
      scalar: p.scalar ?? false, need: p.need ?? '', constant: p.constant ?? false, prior: p.prior ?? false, tree: null, ok: false,
    }
    this.specs.push(s)
    const v = s.value
    if (!v) { this.error('graph.slot', s.node, s.slot, { reason: 'missing' }); return }
    if (v.expr === null && v.ref === null) { this.error('graph.slot', s.node, s.slot, { reason: 'empty' }); return }
    if (v.expr !== null && v.ref !== null) { this.error('graph.slot', s.node, s.slot, { reason: 'both' }); return }
    if (v.ref !== null) { s.ok = true; return }
    const r = bayesParseExpr(v.expr as string, this.catalog)
    if (!r.ok) { this.out.push(finding(r.error.code, 'error', s.node, s.slot, { pos: r.error.pos, len: r.error.len, ...(r.error.args ?? {}) })); return }
    s.tree = r.node
    s.ok = true
  }

  constantOf (key: string): number | null {
    const s = this.specs.find(x => slotKey(x.node, x.slot) === key)
    if (!s || !s.ok || !s.tree) return null
    return constantValue(s.tree)
  }

  /** The ids a slot reads (values) and indexes with, whether or not it types; a name not in the environment is left out. */
  reads (s: SlotSpec): { refs: string[], indices: string[] } {
    if (!s.ok) return { refs: [], indices: [] }
    if (s.value!.ref !== null) return { refs: [s.value!.ref], indices: [] }
    const seenR = new Set<string>()
    const seenI = new Set<string>()
    const walk = (n: BayesExprNode) => {
      if (n.kind === 'name' || n.kind === 'index' || n.kind === 'lag') {
        const sym = this.env.byName.get(n.name)
        if (sym) seenR.add(sym.id)
        if (n.kind === 'index') { const b = this.env.byName.get(n.by); if (b) seenI.add(b.id) }
      }
      if ('args' in n) for (const a of n.args as BayesExprNode[]) walk(a)
    }
    walk(s.tree as BayesExprNode)
    return { refs: sortedKeys(seenR), indices: sortedKeys(seenI) }
  }

  readSets (s: SlotSpec): ReadSets {
    if (!s.ok) return { plain: [], lag: [], index: [] }
    if (s.value!.ref !== null) return this.env.lookupID(s.value!.ref) ? { plain: [s.value!.ref], lag: [], index: [] } : { plain: [], lag: [], index: [] }
    const plain = new Set<string>()
    const lag = new Set<string>()
    const index = new Set<string>()
    const walk = (n: BayesExprNode) => {
      if (n.kind === 'name' || n.kind === 'index' || n.kind === 'lag') {
        const sym = this.env.byName.get(n.name)
        if (sym) (n.kind === 'lag' ? lag : plain).add(sym.id)
        if (n.kind === 'index') { const b = this.env.byName.get(n.by); if (b) index.add(b.id) }
      }
      if ('args' in n) for (const a of n.args as BayesExprNode[]) walk(a)
    }
    walk(s.tree as BayesExprNode)
    return { plain: sortedKeys(plain), lag: sortedKeys(lag), index: sortedKeys(index) }
  }

  typeSlots (): void {
    for (const s of this.specs) {
      if (!s.ok) continue
      const t = this.typeSlot(s)
      if (!t) continue
      const r = this.reads(s)
      t.refs = r.refs
      t.indices = r.indices
      this.slots.set(slotKey(s.node, s.slot), t)
      this.shape(s, t)
      if (s.constant && t.constant === null) this.error('graph.constant', s.node, s.slot)
      if (s.need !== '') this.within(s, t.interval, bayesNamedInterval(s.need, this.catalog), { need: s.need })
      if (t.tree) this.domains(s, t.tree)
    }
  }

  typeSlot (s: SlotSpec): BayesTypedSlot | null {
    const t: BayesTypedSlot = {
      context: s.ctx, tree: null, ref: '', type: { elem: 'real', plate: '' }, interval: bayesUnbounded(),
      constant: null, refs: [], indices: [], lags: [], lifts: [],
    }
    if (s.value!.ref !== null) {
      const id = s.value!.ref
      const sym = this.env.lookupID(id)
      if (!sym) { this.error('graph.slot', s.node, s.slot, { reason: 'unknown_ref', ref: id }); return null }
      if (sym.type.elem !== 'real' && sym.type.elem !== 'int') { this.error('graph.slot', s.node, s.slot, { reason: 'not_number', ref: id }); return null }
      t.ref = id
      t.type = sym.type
      t.interval = sym.interval ?? bayesUnbounded()
      return t
    }
    const c = bayesCheckExpr(s.tree as BayesExprNode, this.env.slotEnv(s.ctx), this.catalog)
    if (!c.ok) {
      this.out.push(finding(c.error.code, 'error', s.node, s.slot, { pos: c.error.pos, len: c.error.len, ...(c.error.args ?? {}) }))
      return null
    }
    t.tree = c.tree
    t.type = c.type
    t.interval = c.interval
    t.lags = c.lags
    t.lifts = c.lifts
    t.constant = constantValue(s.tree)
    return t
  }

  shape (s: SlotSpec, t: BayesTypedSlot): void {
    const p = t.type.plate
    if (p === '') return
    if (s.scalar) this.error('graph.slot_shape', s.node, s.slot, { need: 'scalar', plate: p })
    else if (p !== s.ctx) {
      const args: Record<string, unknown> = { need: 'context', plate: p }
      if (s.ctx !== '') args.context = s.ctx
      this.error('graph.slot_shape', s.node, s.slot, args)
    }
  }

  within (s: SlotSpec, have: BayesExprInterval, need: BayesExprInterval, args: Record<string, unknown>): void {
    const rel = bayesIntervalWithin(have, need)
    if (rel === 'disjoint') this.error('graph.slot_interval', s.node, s.slot, args)
    else if (rel === 'overlap') this.add('warning', 'graph.slot_maybe', s.node, s.slot, args)
  }

  domains (s: SlotSpec, t: BayesExprTyped): void {
    const seen = new Set<string>()
    const walk = (x: BayesExprTyped) => {
      for (const a of x.args) walk(a)
      if (x.node.kind !== 'call' || x.args.length !== 1) return
      const name = x.node.name
      const f = this.catalog.language.functions.find(g => g.name === name)
      if (!f || !f.domain || f.domain === 'real') return
      const have = bayesTypedInterval(x.args[0], this.catalog)
      const need = bayesNamedInterval(f.domain, this.catalog)
      const rel = bayesIntervalWithin(have, need)
      const key = `${f.name}/${rel}`
      if (rel === 'inside' || seen.has(key)) return
      seen.add(key)
      this.within(s, have, need, { need: f.domain, function: f.name })
    }
    walk(t)
  }

  priorData (): void {
    const through = new Map<string, string[]>()
    const push = (k: string, ids: string[]) => { const l = through.get(k) ?? []; l.push(...ids); through.set(k, l) }
    for (const s of this.specs) {
      const k = s.owner.kind
      if (k === 'transformed' || k === 'linear_predictor') push(s.owner.id, this.reads(s).refs)
    }
    for (const n of this.nodeList()) if (n.kind === 'linear_predictor') push(n.id, n.terms.map(t => t.data))
    const isData = (id: string) => this.env.nodes.get(id)?.kind === 'data'
    const isVia = (id: string) => { const k = this.env.nodes.get(id)?.kind; return k === 'transformed' || k === 'linear_predictor' }
    const minData = (start: string): string => {
      let best = ''
      const seen = new Set<string>([start])
      const queue = [start]
      while (queue.length) {
        const cur = queue.shift() as string
        for (const r of through.get(cur) ?? []) {
          if (isData(r)) { if (best === '' || r < best) best = r } else if (isVia(r) && !seen.has(r)) { seen.add(r); queue.push(r) }
        }
      }
      return best
    }
    for (const s of this.specs) {
      if (!s.prior) continue
      let data = ''
      let via = ''
      for (const r of this.reads(s).refs) {
        let d = ''
        let w = ''
        if (isData(r)) d = r
        else if (isVia(r)) { d = minData(r); w = r }
        if (d !== '' && (data === '' || d < data || (d === data && w < via))) { data = d; via = w }
      }
      if (data === '') continue
      const args: Record<string, unknown> = { data }
      if (via !== '') args.through = via
      this.error('graph.prior_data', s.node, s.slot, args)
    }
  }

  // ---- what a model is made of

  modelRules (): void {
    this.platesTotal()
    this.likelihoods()
    this.terms()
    this.effects()
    this.states()
    this.deps = this.dependencies()
    this.cycles(this.deps)
    this.selfPredictors()
    this.unused()
    this.formCentred()
    this.plateEmpty()
  }

  specByKey (): Map<string, SlotSpec> {
    const m = new Map<string, SlotSpec>()
    for (const s of this.specs) { const k = slotKey(s.node, s.slot); if (!m.has(k)) m.set(k, s) }
    return m
  }

  platesTotal (): void {
    const stored = this.g.plates.length
    if (stored > this.lim('max_plates')) return
    let total = stored
    for (const n of this.env.nodes.values()) if (n.kind === 'data' && n.type === 'category') total++
    if (total > this.lim('max_plates')) this.out.push(tooBig('plates', total, this.lim('max_plates')))
  }

  likelihoods (): void {
    const ls = this.nodeList().filter(n => n.kind === 'likelihood')
    if (ls.length === 0) { this.error('graph.likelihood', '', '', { reason: 'none' }); return }
    const byObserved = new Map<string, string[]>()
    for (const l of ls) {
      const o = this.env.nodes.get(l.observed)
      if (o && o.kind === 'data') { const a = byObserved.get(o.id) ?? []; a.push(l.id); byObserved.set(o.id, a) }
    }
    const specs = this.specByKey()
    for (const l of ls) {
      let obs = this.env.nodes.get(l.observed)
      if (l.observed === '') this.error('graph.likelihood', l.id, 'observed', { reason: 'missing' })
      else if (!obs) this.error('graph.likelihood', l.id, 'observed', { reason: 'unknown_node', observed: l.observed })
      else if (obs.kind !== 'data') this.error('graph.likelihood', l.id, 'observed', { reason: 'not_data', observed: l.observed })
      else if ((byObserved.get(obs.id) ?? []).length > 1) {
        this.error('graph.likelihood', l.id, 'observed', { reason: 'observed_twice', observed: obs.id, count: (byObserved.get(obs.id) ?? []).length })
      }
      if (!obs || obs.kind !== 'data') obs = undefined
      const d = this.likelihoodDist(l)
      if (!d) continue
      if (obs) {
        const { need, fits } = outcomeNeed(d)
        const kind = outcomeKind(obs)
        if (kind !== '' && !fits.includes(kind)) {
          const args: Record<string, unknown> = { reason: 'outcome', dist: d.id, need, type: obs.type }
          if (obs.type === 'int') args.support = obs.support
          this.error('graph.likelihood', l.id, 'observed', args)
        }
      }
      for (const p of this.param(d)) {
        if (!p.data) continue
        const s = specs.get(slotKey(l.id, 'args.' + p.name))
        if (s && s.ok) this.dataArgument(l, obs, d, p.name, s)
      }
    }
  }

  likelihoodDist (l: BayesGraphNode): BayesDistribution | null {
    if (l.dist === '') { this.error('graph.likelihood', l.id, 'dist', { reason: 'missing' }); return null }
    const d = this.dist(l.dist)
    if (!d) { this.error('graph.likelihood', l.id, 'dist', { reason: 'unknown_dist', dist: l.dist }); return null }
    if (!d.uses.includes('likelihood')) { this.error('graph.likelihood', l.id, 'dist', { reason: 'not_likelihood', dist: d.id }); return null }
    this.arguments(l, 'args.', d, l.args, 'graph.likelihood')
    return d
  }

  arguments (n: BayesGraphNode, prefix: string, d: BayesDistribution, args: Record<string, BayesGraphSlot>, code: string): void {
    const want = new Set<string>()
    for (const p of this.param(d)) {
      want.add(p.name)
      if (!(p.name in args)) this.error(code, n.id, prefix + p.name, { reason: 'missing_arg', dist: d.id, arg: p.name })
    }
    for (const arg of Object.keys(args)) {
      if (!want.has(arg)) this.error(code, n.id, prefix + arg, { reason: 'unknown_arg', dist: d.id, arg })
    }
  }

  dataArgument (l: BayesGraphNode, obs: BayesGraphNode | undefined, d: BayesDistribution, arg: string, s: SlotSpec): void {
    const slot = 'args.' + arg
    let id = ''
    if (s.value!.ref !== null) id = s.value!.ref
    else if (s.tree && s.tree.kind === 'name') {
      const sym = this.env.byName.get(s.tree.name)
      if (!sym) return
      id = sym.id
    }
    const n = this.env.nodes.get(id)
    if (!n || n.kind !== 'data') {
      if (!this.env.lookupID(id) && s.value!.ref !== null) return
      this.error('graph.likelihood', l.id, slot, { reason: 'arg_not_data', dist: d.id, arg })
      return
    }
    const k = outcomeKind(n)
    if (k !== 'count' && k !== 'binary') {
      const args: Record<string, unknown> = { reason: 'arg_type', dist: d.id, arg, type: n.type }
      if (n.type === 'int') args.support = n.support
      this.error('graph.likelihood', l.id, slot, args)
      return
    }
    if (obs && plateOf(n) !== plateOf(obs)) {
      this.error('graph.likelihood', l.id, slot, { reason: 'arg_plate', dist: d.id, arg, plate: plateOf(n), observed: plateOf(obs) })
    }
  }

  terms (): void {
    const specs = this.specByKey()
    for (const n of this.nodeList()) {
      if (n.kind !== 'linear_predictor') continue
      const count = new Map<string, number>()
      for (const t of n.terms) count.set(t.data, (count.get(t.data) ?? 0) + 1)
      n.terms.forEach((t, i) => {
        const dataSlot = `terms[${i}].data`
        const coefSlot = `terms[${i}].coef`
        const d = this.env.nodes.get(t.data)
        if (!d || d.kind !== 'data') { this.error('graph.terms', n.id, dataSlot, { reason: 'not_data', data: t.data }); return }
        if ((count.get(t.data) ?? 0) > 1) this.error('graph.terms', n.id, dataSlot, { reason: 'duplicate', data: t.data, count: count.get(t.data) })
        if (d.type === 'group_index' || d.type === 'time') { this.error('graph.terms', n.id, dataSlot, { reason: 'data_type', data: t.data, type: d.type }); return }
        if (plateOf(d) !== plateOf(n)) this.error('graph.terms', n.id, dataSlot, { reason: 'plate', data: t.data, plate: plateOf(d), expected: plateOf(n) })
        if (d.type !== 'real' && d.type !== 'int' && d.type !== 'category') return
        const s = specs.get(slotKey(n.id, coefSlot))
        if (!s || !s.ok) return
        if (!this.slots.has(slotKey(s.node, s.slot))) return
        const p = this.coefficient(s)
        if (!p) { this.error('graph.terms', n.id, coefSlot, { reason: 'coef_not_parameter', data: t.data }); return }
        let need = 'scalar'
        let plate = ''
        if (d.type === 'category') { need = 'levels'; plate = bayesLevelsPlate(d.id) }
        if (plateOf(p) !== plate) this.error('graph.terms', n.id, coefSlot, { reason: 'coef_plate', data: t.data, need, plate: plateOf(p) })
      })
    }
  }

  coefficient (s: SlotSpec): BayesGraphNode | null {
    let id = ''
    if (s.value!.ref !== null) id = s.value!.ref
    else if (s.tree && s.tree.kind === 'name') {
      const sym = this.env.byName.get(s.tree.name)
      if (!sym) return null
      id = sym.id
    }
    const n = this.env.nodes.get(id)
    return n && n.kind === 'parameter' ? n : null
  }

  effects (): void {
    for (const n of this.nodeList()) {
      if (n.kind !== 'correlated_effects') continue
      const k = n.components.length
      if (k < 2 || k > this.lim('max_effect_components')) {
        this.error('graph.effects', n.id, 'components', { reason: 'count', got: k, min: 2, max: this.lim('max_effect_components') })
      }
      if (!n.correlation) this.error('graph.effects', n.id, 'correlation', { reason: 'missing' })
    }
  }

  states (): void {
    for (const n of this.nodeList()) {
      if (n.kind !== 'state') continue
      const ep = this.env.plates.get(plateOf(n))
      if (ep && ep.kind === 'rows' && !ep.series) this.error('graph.state', n.id, 'plate', { reason: 'not_series', plate: plateOf(n) })
      if (n.process !== 'random_walk') this.error('graph.state', n.id, 'process', { reason: 'process', process: n.process })
      if (n.form !== '' && n.form !== 'non_centred' && n.form !== 'centred') this.error('graph.state', n.id, 'form', { reason: 'form', form: n.form })
      this.stateInit(n)
    }
  }

  stateInit (n: BayesGraphNode): void {
    if (!n.init) { this.error('graph.state', n.id, 'init', { reason: 'missing' }); return }
    const d = this.dist(n.init.dist)
    if (!d) { this.error('graph.state', n.id, 'init', { reason: 'unknown_dist', dist: n.init.dist }); return }
    if (d.support === 'cholesky_factor_corr') { this.error('graph.state', n.id, 'init', { reason: 'effects_only', dist: d.id }); return }
    if (!d.uses.includes('prior')) { this.error('graph.state', n.id, 'init', { reason: 'not_prior', dist: d.id }); return }
    this.arguments(n, 'init.', d, n.init.args, 'graph.state')
  }

  // ---- dependencies

  dependencies (): Deps {
    const seen = new Set<string>()
    const edges: BayesEdge[] = []
    const add = (from: string, to: string, node: string, slot: string, kind: BayesEdge['kind']) => {
      const k = [from, to, node, slot, kind].join('\u0001')
      if (!seen.has(k)) { seen.add(k); edges.push({ from, to, node, slot, kind }) }
    }
    for (const s of this.specs) {
      const r = this.readSets(s)
      for (const id of r.plain) add(id, s.owner.id, s.node, s.slot, 'read')
      for (const id of r.lag) add(id, s.owner.id, s.node, s.slot, 'lag')
      for (const id of r.index) add(id, s.owner.id, s.node, s.slot, 'index')
    }
    for (const n of this.nodeList()) {
      if (n.kind === 'linear_predictor') {
        n.terms.forEach((t, i) => { if (this.env.nodes.has(t.data)) add(t.data, n.id, n.id, `terms[${i}].data`, 'term') })
      } else if (n.kind === 'likelihood') {
        const o = this.env.nodes.get(n.observed)
        if (o && o.kind === 'data') add(o.id, n.id, n.id, 'observed', 'observed')
      }
    }
    edges.sort((a, b) => cmp(a.to, b.to) || cmp(a.node, b.node) || cmp(a.slot, b.slot) || cmp(a.from, b.from) || cmp(a.kind, b.kind))
    const all = new Map<string, Set<string>>()
    const pl = new Map<string, Set<string>>()
    const put = (m: Map<string, Set<string>>, to: string, from: string) => { let s = m.get(to); if (!s) { s = new Set(); m.set(to, s) } s.add(from) }
    for (const e of edges) {
      const from = ownerOf(e.from)
      if (!this.env.nodes.has(from)) continue
      put(all, e.to, from)
      if (e.kind === 'read' || e.kind === 'term') put(pl, e.to, from)
    }
    const d: Deps = { edges, reads: new Map(), plain: new Map() }
    for (const [to, m] of all) d.reads.set(to, sortedKeys(m))
    for (const [to, m] of pl) d.plain.set(to, sortedKeys(m))
    return d
  }

  nameOrLabel (id: string): string {
    const n = this.env.nodes.get(id)
    if (!n) return id
    return n.name !== null && n.name !== '' ? n.name : n.label
  }

  cycles (d: Deps): void {
    for (const comp of stronglyConnected(this.sortedIDs(), d.reads)) {
      const start = comp[0]
      const inn = new Set(comp)
      const path = shortestCycle(start, d.reads, inn)
      if (!path) continue
      let node = start
      let slot = ''
      for (const e of d.edges) {
        if (e.to === path[0] && ownerOf(e.from) === path[1 % path.length]) { node = e.node; slot = e.slot; break }
      }
      this.error('graph.cycle', node, slot, { names: path.map(id => this.nameOrLabel(id)), cycle: path })
    }
  }

  selfPredictors (): void {
    const via = (id: string) => { const k = this.env.nodes.get(id)?.kind; return k === 'transformed' || k === 'linear_predictor' }
    for (const l of this.nodeList()) {
      const o = this.env.nodes.get(l.observed)
      if (l.kind !== 'likelihood' || !o || o.kind !== 'data') continue
      const reaches = (start: string): boolean => {
        const seen = new Set<string>([start])
        const queue = [start]
        while (queue.length) {
          const cur = queue.shift() as string
          for (const r of this.deps.plain.get(cur) ?? []) {
            if (r === o.id) return true
            if (via(r) && !seen.has(r)) { seen.add(r); queue.push(r) }
          }
        }
        return false
      }
      for (const s of this.specs) {
        if (s.owner !== l) continue
        let direct = false
        let through = ''
        for (const id of this.readSets(s).plain) {
          const r = ownerOf(id)
          if (r === o.id) direct = true
          else if (via(r) && reaches(r) && (through === '' || r < through)) through = r
        }
        if (direct) this.error('graph.self_predictor', l.id, s.slot, { observed: o.id })
        else if (through !== '') this.error('graph.self_predictor', l.id, s.slot, { observed: o.id, through })
      }
    }
  }

  unused (): void {
    const used = new Set<string>()
    for (const e of this.deps.edges) used.add(ownerOf(e.from))
    for (const p of this.g.plates) {
      if (p.key !== '') used.add(p.key)
      if (p.series) used.add(p.series.time)
    }
    for (const n of this.nodeList()) if (n.kind === 'data' && !used.has(n.id)) this.error('graph.unused_data', n.id, '')
    const reach = new Set<string>()
    const seen: string[] = []
    for (const n of this.nodeList()) if (n.kind === 'likelihood') { reach.add(n.id); seen.push(n.id) }
    if (seen.length === 0) return
    while (seen.length) {
      const cur = seen.pop() as string
      for (const w of this.deps.reads.get(cur) ?? []) if (!reach.has(w)) { reach.add(w); seen.push(w) }
    }
    for (const n of this.nodeList()) if (n.kind === 'parameter' && !reach.has(n.id)) this.add('warning', 'graph.unused_parameter', n.id, '')
  }

  formCentred (): void {
    const groupForm = new Map<string, string>()
    for (const p of this.g.plates) if (p.kind === 'group' && !groupForm.has(p.id)) groupForm.set(p.id, p.form)
    for (const n of this.nodeList()) {
      if (n.kind === 'parameter') {
        const plate = plateOf(n)
        if (!groupForm.has(plate) || groupForm.get(plate) === 'centred' || !n.prior) continue
        const bounded = n.bounds !== null && (n.bounds.lower !== null || n.bounds.upper !== null)
        if (bounded) this.add('warning', 'graph.form_centred', n.id, 'bounds', { reason: 'bounded', plate })
        const d = this.dist(n.prior.dist)
        if (!d || !d.uses.includes('prior') || d.support === 'cholesky_factor_corr') continue
        const locScale = Boolean(d.location_scale)
        if (!locScale) this.add('warning', 'graph.form_centred', n.id, 'prior', { reason: 'not_location_scale', dist: d.id, plate })
        const p = this.params.get(n.id)
        if (p) p.nonCentred = locScale && !bounded
      } else if (n.kind === 'state') {
        if (!n.init || n.form === 'centred') continue
        const d = this.dist(n.init.dist)
        if (!d || !d.uses.includes('prior') || d.support === 'cholesky_factor_corr') continue
        if (!d.location_scale) this.add('warning', 'graph.form_centred', n.id, 'init', { reason: 'not_location_scale', dist: d.id })
      }
    }
  }

  plateEmpty (): void {
    const on = new Set<string>()
    for (const n of this.g.nodes) if (n.plate !== null) on.add(n.plate)
    const ids = sortedStrings(this.g.plates.filter(p => p.kind === 'group' && !on.has(p.id)).map(p => p.id))
    ids.forEach((id, i) => { if (i === 0 || ids[i - 1] !== id) this.add('warning', 'graph.plate_empty', id, '') })
  }
}

function outcomeKind (n: BayesGraphNode): string {
  switch (n.type) {
    case 'real': return 'real'
    case 'int':
      if (n.support === 'binary') return 'binary'
      if (n.support === 'nonnegative') return 'count'
      if (n.support === 'any') return 'integer'
      return ''
    case 'group_index': case 'category': case 'time': return 'other'
  }
  return ''
}

function outcomeNeed (d: BayesDistribution): { need: string, fits: string[] } {
  switch (d.outcome) {
    case 'real': return { need: 'real', fits: ['real'] }
    case 'binary': return { need: 'binary', fits: ['binary'] }
    case 'zero_to_n': case 'counts': return { need: 'count', fits: ['count', 'binary'] }
  }
  return { need: '', fits: [] }
}

/** Tarjan: the strongly connected components, each sorted, by their smallest id. */
function stronglyConnected (ids: string[], reads: Map<string, string[]>): string[][] {
  const index = new Map<string, number>()
  const low = new Map<string, number>()
  const on = new Set<string>()
  const stack: string[] = []
  const out: string[][] = []
  let next = 0
  const visit = (id: string) => {
    index.set(id, next); low.set(id, next); next++
    stack.push(id); on.add(id)
    for (const w of reads.get(id) ?? []) {
      if (!index.has(w)) {
        visit(w)
        if ((low.get(w) as number) < (low.get(id) as number)) low.set(id, low.get(w) as number)
      } else if (on.has(w) && (index.get(w) as number) < (low.get(id) as number)) low.set(id, index.get(w) as number)
    }
    if (low.get(id) === index.get(id)) {
      const comp: string[] = []
      for (;;) {
        const w = stack.pop() as string
        on.delete(w)
        comp.push(w)
        if (w === id) break
      }
      out.push(comp.sort(cmp))
    }
  }
  for (const id of ids) if (!index.has(id)) visit(id)
  return out.sort((a, b) => cmp(a[0], b[0]))
}

function shortestCycle (start: string, reads: Map<string, string[]>, inn: Set<string>): string[] | null {
  const parent = new Map<string, string>()
  const queue = [start]
  const seen = new Set<string>()
  while (queue.length) {
    let cur = queue.shift() as string
    for (const w of reads.get(cur) ?? []) {
      if (!inn.has(w)) continue
      if (w === start) {
        const path = [cur]
        while (cur !== start) { cur = parent.get(cur) as string; path.push(cur) }
        return path.reverse()
      }
      if (!seen.has(w)) { seen.add(w); parent.set(w, cur); queue.push(w) }
    }
  }
  return null
}

/** A topological order (what a node reads comes first), ties by the smallest id; the nodes of a cycle and their readers end it, by id. */
function topological (ids: string[], reads: Map<string, string[]>): string[] {
  const pending = new Map<string, number>()
  const readers = new Map<string, string[]>()
  for (const id of ids) {
    for (const w of reads.get(id) ?? []) {
      pending.set(id, (pending.get(id) ?? 0) + 1)
      if (w !== id) { const l = readers.get(w) ?? []; l.push(id); readers.set(w, l) }
    }
  }
  const ready = ids.filter(id => (pending.get(id) ?? 0) === 0)
  const out: string[] = []
  const done = new Set<string>()
  while (ready.length) {
    let m = 0
    for (let i = 1; i < ready.length; i++) if (ready[i] < ready[m]) m = i
    const id = ready.splice(m, 1)[0]
    out.push(id)
    done.add(id)
    for (const r of readers.get(id) ?? []) {
      const p = (pending.get(r) as number) - 1
      pending.set(r, p)
      if (p === 0) ready.push(r)
    }
  }
  for (const id of ids) if (!done.has(id)) out.push(id)
  return out
}

// ---------------------------------------------------------------- the validated model

export interface BayesModelPlate {
  id: string
  kind: 'rows' | 'group' | 'levels'
  label: string
  parent: string
  depth: number
  key: string
  category: string
  nonCentred: boolean
}

export interface BayesBinding {
  node: string
  name: string
  label: string
  type: string
  support: string
  plate: string
  target: string
  standardize: boolean
  observed: boolean
}

export interface BayesScalePair { node: string, term: number, data: string, intercept: boolean }

/**
 * The graph with what the validator learnt: with errors it holds what could be read (the order of
 * a cyclic graph ends with the nodes of the cycles, a slot that does not type is missing from
 * `slots`). With a graph.id or graph.rows finding it holds the findings alone.
 */
export interface BayesModel {
  graph: BayesGraph
  findings: BayesGraphFinding[]
  /** False when the structure could not be trusted (graph.id, graph.rows): nothing below was computed. */
  built: boolean
  plates: BayesModelPlate[]
  order: string[]
  slots: Map<string, BayesTypedSlot>
  params: Map<string, BayesParam>
  edges: BayesEdge[]
  lags: number[]
  lifts: BayesExprLift[]
  bindings: BayesBinding[]
  pairs: BayesScalePair[]
  states: Map<string, { nonCentred: boolean }>
  effects: Map<string, { plate: string, components: number, nonCentred: boolean }>
}

/** The key of a slot in `BayesModel.slots`. */
export const bayesSlotKey = slotKey

/** No error finding: a program can be written from it. */
export const bayesModelValid = (m: BayesModel): boolean => m.findings.every(f => f.severity !== 'error')

/** Validates the graph: every finding, sorted by (node, slot, code). With a graph.id or graph.rows finding only the structural ones are returned. */
export function bayesGraphValidate (g: BayesGraph, catalog: BayesCatalog = BAYES_CATALOG): BayesGraphFinding[] {
  return new Validation(g, catalog).run().out
}

/** Validates the graph and gathers what the previews and the canvas read. Never computes a canonical form or a hash. */
export function bayesGraphModel (g: BayesGraph, catalog: BayesCatalog = BAYES_CATALOG): BayesModel {
  const v = new Validation(g, catalog).run()
  const m: BayesModel = {
    graph: g, findings: v.out, built: Boolean(v.env), plates: [], order: [], slots: new Map(), params: new Map(), edges: [],
    lags: [], lifts: [], bindings: [], pairs: [], states: new Map(), effects: new Map(),
  }
  if (!v.env) return m
  m.slots = v.slots
  m.edges = v.deps.edges
  m.order = topological(v.sortedIDs(), v.deps.reads)
  m.plates = modelPlates(v)
  m.params = roles(v)
  const groupForm = new Map<string, string>()
  for (const p of g.plates) if (p.kind === 'group' && !groupForm.has(p.id)) groupForm.set(p.id, p.form)
  for (const n of v.nodeList()) {
    if (n.kind === 'state') {
      const d = n.init ? v.dist(n.init.dist) : undefined
      m.states.set(n.id, { nonCentred: Boolean(n.init && n.form !== 'centred' && d && d.uses.includes('prior') && d.location_scale) })
    } else if (n.kind === 'correlated_effects') {
      m.effects.set(n.id, { plate: plateOf(n), components: n.components.length, nonCentred: groupForm.get(plateOf(n)) !== 'centred' })
    }
  }
  const lifts = new Map<string, BayesExprLift>()
  const lags = new Set<number>()
  for (const t of v.slots.values()) {
    for (const l of t.lifts) lifts.set([l.index, l.id, l.rows, l.context].join('\u0001'), l)
    for (const k of t.lags) lags.add(k)
  }
  m.lifts = [...lifts.values()].sort((a, b) => cmp(a.id, b.id) || cmp(a.context, b.context) || cmp(a.rows, b.rows) || cmp(a.index, b.index))
  m.lags = [...lags].sort((a, b) => a - b)
  const observed = new Set(v.nodeList().filter(n => n.kind === 'likelihood').map(n => n.observed))
  for (const n of v.nodeList()) {
    if (n.kind === 'data') {
      m.bindings.push({
        node: n.id, name: n.name ?? '', label: n.label, type: n.type, support: n.support, plate: plateOf(n), target: n.target,
        standardize: n.standardize && n.type === 'real', observed: observed.has(n.id),
      })
    } else if (n.kind === 'linear_predictor') {
      let standardized = false
      n.terms.forEach((t, i) => {
        const d = v.env.nodes.get(t.data)
        if (d && d.kind === 'data' && d.type === 'real' && d.standardize) { m.pairs.push({ node: n.id, term: i, data: d.id, intercept: false }); standardized = true }
      })
      if (standardized) m.pairs.push({ node: n.id, term: -1, data: '', intercept: true })
    }
  }
  return m
}

function modelPlates (v: Validation): BayesModelPlate[] {
  const byID = new Map<string, BayesGraphPlate>()
  for (const p of v.g.plates) if (!byID.has(p.id)) byID.set(p.id, p)
  const depth = (p: BayesGraphPlate): number => {
    let d = 1
    const seen = new Set<string>([p.id])
    let cur = p
    while (cur.parent !== null) {
      const next = byID.get(cur.parent)
      if (!next || next.kind !== 'group' || seen.has(next.id)) break
      seen.add(next.id)
      d++
      cur = next
    }
    return d
  }
  const rows: BayesModelPlate[] = []
  const groups: BayesModelPlate[] = []
  const levels: BayesModelPlate[] = []
  for (const id of sortedStrings(byID.keys())) {
    const p = byID.get(id) as BayesGraphPlate
    if (p.kind === 'rows') rows.push({ id: p.id, kind: 'rows', label: p.label, parent: '', depth: 0, key: '', category: '', nonCentred: false })
    else if (p.kind === 'group') {
      groups.push({ id: p.id, kind: 'group', label: p.label, parent: p.parent ?? '', depth: depth(p), key: p.key, category: '', nonCentred: p.form !== 'centred' })
    }
  }
  groups.sort((a, b) => a.depth - b.depth || cmp(a.id, b.id))
  for (const n of v.nodeList()) {
    if (n.kind === 'data' && n.type === 'category') {
      levels.push({ id: bayesLevelsPlate(n.id), kind: 'levels', label: n.label, parent: '', depth: 0, key: '', category: n.id, nonCentred: false })
    }
  }
  return [...rows, ...groups, ...levels]
}

/** The parameters with their role: a parameter whose value feeds another prior (directly or through transformed and linear-predictor nodes) is a hyperprior. */
function roles (v: Validation): Map<string, BayesParam> {
  const hyper = new Set<string>()
  const queue: string[] = []
  const seen = new Set<string>()
  const push = (id: string) => { id = ownerOf(id); if (!seen.has(id)) { seen.add(id); queue.push(id) } }
  for (const s of v.specs) {
    if (!s.prior) continue
    const r = v.readSets(s)
    for (const ids of [r.plain, r.lag, r.index]) for (const id of ids) push(id)
  }
  while (queue.length) {
    const id = queue.shift() as string
    const n = v.env.nodes.get(id)
    if (!n) continue
    if (n.kind === 'parameter') hyper.add(id)
    else if (n.kind === 'transformed' || n.kind === 'linear_predictor') for (const w of v.deps.reads.get(id) ?? []) push(w)
  }
  const out = new Map<string, BayesParam>()
  for (const [id, p] of v.params) out.set(id, { ...p, role: hyper.has(id) ? 'hyperprior' : 'prior' })
  return out
}

/** Reads, decodes and validates a document in one step: the model, or the findings of a document that is not a graph. */
export function bayesGraphReadText (text: string, catalog: BayesCatalog = BAYES_CATALOG): { model: BayesModel | null, findings: BayesGraphFinding[] } {
  const { graph, findings } = bayesGraphDecode(text)
  if (!graph) return { model: null, findings }
  const model = bayesGraphModel(graph, catalog)
  return { model, findings: model.findings }
}

// ---------------------------------------------------------------- messages

/**
 * The key of a finding's message below `bayes.graph.` (`plate_key.missing`, `slot_shape.single`,
 * `cycle`), chosen from its code, slot and arguments; null for an `expr.*` finding, whose messages
 * are `bayes.expr.*`. The arguments are the placeholders ({dist}, {arg}, {plate}…); a plate or a
 * node is an id there, which the editor replaces by its label before formatting.
 */
export function bayesGraphMessageKey (f: BayesGraphFinding): string | null {
  if (f.code.startsWith('expr.')) return null
  const code = f.code.replace(/^graph\./, '')
  const a = f.args ?? {}
  const reason = String(a.reason ?? '')
  switch (code) {
    case 'too_big': return `too_big.${a.what}`
    case 'series': return f.slot === 'series.by' ? 'series.by' : 'series.time'
    case 'likelihood': return reason === 'missing' && f.slot === 'dist' ? 'likelihood.dist_missing' : `likelihood.${reason}`
    case 'terms': return reason === 'coef_plate' ? `terms.coef_plate.${a.need}` : `terms.${reason}`
    case 'slot_shape': return a.need === 'scalar' ? 'slot_shape.scalar' : a.context === undefined ? 'slot_shape.single' : 'slot_shape.context'
    case 'slot_interval': case 'slot_maybe': return a.function !== undefined ? `${code}.function.${a.need}` : `${code}.${a.need}`
    case 'prior_data': case 'self_predictor': return a.through !== undefined ? `${code}.through` : `${code}.direct`
    case 'format': case 'rows': case 'name_taken': case 'name_function': case 'plate_empty': case 'constant': case 'cycle':
    case 'unused_data': case 'unused_parameter':
      return code
    default: return `${code}.${reason}`
  }
}

// ---------------------------------------------------------------- the prior plan

/** A typed expression, with the ids the graph gave its names (no name, no label). */
export type BayesPlanAst =
  | { kind: 'num', num: number }
  | { kind: 'name', id: string }
  | { kind: 'index', id: string, by: string, lifted: boolean }
  | { kind: 'lag', id: string, k: number }
  | { kind: 'call', name: string, args: BayesPlanAst[] }
  | { kind: 'neg', args: [BayesPlanAst] }
  | { kind: 'binary', op: '+' | '-' | '*' | '/' | '^', args: [BayesPlanAst, BayesPlanAst] }

export type BayesPlanSlot = { ast: BayesPlanAst } | { ref: string } | { constant: number }

export interface BayesPlanNode {
  /** The node's id; `<node>.<component>`, `<node>.correlation`, `<state>.init` and `<state>.scale` for the parts of one. */
  id: string
  kind: 'parameter' | 'component' | 'correlation' | 'state_init' | 'state_scale' | 'transformed'
  /** The plate the value is per unit of ('' a single value, a group plate id, `levels:<node>`). */
  plate: string
  dist?: string
  args?: Record<string, BayesPlanSlot>
  range?: { lower?: number, upper?: number }
  truncated?: boolean
  value?: BayesPlanSlot
  k?: number
  eta?: number
  /** The error findings on this node or an ancestor (a node with errors is not drawn). */
  errors: number
}

export interface BayesPriorPlan {
  /** Every plannable node, ancestors first (the model's order). */
  order: string[]
  nodes: Record<string, BayesPlanNode>
}

function planAst (t: BayesExprTyped): BayesPlanAst {
  const n = t.node
  switch (n.kind) {
    case 'num': return { kind: 'num', num: n.num }
    case 'name': return { kind: 'name', id: t.sym?.id ?? '' }
    case 'index': return { kind: 'index', id: t.sym?.id ?? '', by: t.by?.id ?? '', lifted: t.lifted }
    case 'lag': return { kind: 'lag', id: t.sym?.id ?? '', k: n.k }
    case 'call': return { kind: 'call', name: n.name, args: t.args.map(planAst) }
    case 'neg': return { kind: 'neg', args: [planAst(t.args[0])] }
    case 'binary': return { kind: 'binary', op: n.op, args: [planAst(t.args[0]), planAst(t.args[1])] }
  }
}

/**
 * The plan the prior preview draws from: every parameter, transformed node, component and
 * correlation (and the init and scale of a state) whose value needs no data, with its
 * distribution, its typed arguments, its range and truncation, and the count of error findings on it
 * or on an ancestor, ancestors first in the model's order. Plain JSON: no data, no label, nothing a
 * program depends on. Empty when the structure could not be trusted.
 */
export function bayesPriorPlan (model: BayesModel, catalog: BayesCatalog = BAYES_CATALOG): BayesPriorPlan {
  const plan: BayesPriorPlan = { order: [], nodes: {} }
  if (!model.built) return plan
  const nodes = new Map<string, BayesGraphNode>()
  for (const n of model.graph.nodes) if (!nodes.has(n.id)) nodes.set(n.id, n)

  const slotOf = (node: string, slot: string): BayesPlanSlot | undefined => {
    const t = model.slots.get(slotKey(node, slot))
    if (!t) return undefined
    if (t.constant !== null) return { constant: t.constant }
    if (t.ref !== '') return { ref: t.ref }
    return t.tree ? { ast: planAst(t.tree) } : undefined
  }
  interface Cand { node: BayesPlanNode, owner: string, reads: string[], match: (f: BayesGraphFinding) => boolean }
  const cands = new Map<string, Cand>()
  const readsOf = (owner: string, accept: (slot: string) => boolean, slotNode?: string): string[] => {
    const out = new Set<string>()
    for (const e of model.edges) {
      if (e.to !== owner || (slotNode !== undefined && e.node !== slotNode) || !accept(e.slot)) continue
      if (e.kind === 'index') continue
      out.add(e.from)
    }
    return sortedKeys(out)
  }
  const ownErrors = (owner: string, accept: (slot: string) => boolean) => (f: BayesGraphFinding) =>
    f.severity === 'error' && f.node === owner && f.code !== 'graph.label' && f.code !== 'graph.note' && accept(f.slot)
  const any = () => true

  for (const n of model.graph.nodes) {
    if (nodes.get(n.id) !== n) continue
    const plate = plateOf(n)
    if (n.kind === 'parameter') {
      const p = model.params.get(n.id)
      const pn: BayesPlanNode = { id: n.id, kind: 'parameter', plate, errors: 0 }
      if (n.prior) {
        pn.dist = n.prior.dist
        const d = catalog.distributions.find(x => x.id === n.prior!.dist)
        const args: Record<string, BayesPlanSlot> = {}
        for (const a of d ? (d.params as Array<{ name: string }>).map(x => x.name) : []) {
          const s = slotOf(n.id, 'prior.' + a)
          if (s) args[a] = s
        }
        pn.args = args
      }
      if (p) {
        const r: { lower?: number, upper?: number } = {}
        if (Number.isFinite(p.range.lo)) r.lower = p.range.lo
        if (Number.isFinite(p.range.hi)) r.upper = p.range.hi
        pn.range = r
        pn.truncated = p.truncated
      }
      cands.set(n.id, { node: pn, owner: n.id, reads: readsOf(n.id, any), match: ownErrors(n.id, any) })
    } else if (n.kind === 'transformed') {
      const pn: BayesPlanNode = { id: n.id, kind: 'transformed', plate, errors: 0 }
      const v = slotOf(n.id, 'value')
      if (v) pn.value = v
      cands.set(n.id, { node: pn, owner: n.id, reads: readsOf(n.id, any), match: ownErrors(n.id, any) })
    } else if (n.kind === 'correlated_effects') {
      for (const k of n.components) {
        const id = `${n.id}.${k.id}`
        const pn: BayesPlanNode = { id, kind: 'component', plate, dist: 'normal', errors: 0 }
        const args: Record<string, BayesPlanSlot> = {}
        const m = slotOf(id, 'mean')
        const s = slotOf(id, 'scale')
        if (m) args.mu = m
        if (s) args.sigma = s
        pn.args = args
        cands.set(id, { node: pn, owner: n.id, reads: readsOf(n.id, any, id), match: ownErrors(id, any) })
      }
      if (n.correlation) {
        const id = `${n.id}.correlation`
        const pn: BayesPlanNode = { id, kind: 'correlation', plate, dist: 'lkj_corr_cholesky', k: n.components.length, errors: 0 }
        const e = model.slots.get(slotKey(id, 'eta'))
        if (e && e.constant !== null) pn.eta = e.constant
        const eraseNode = ownErrors(id, any)
        cands.set(id, { node: pn, owner: n.id, reads: readsOf(n.id, any, id), match: f => eraseNode(f) || (f.severity === 'error' && f.node === n.id && f.code === 'graph.effects') })
      }
    } else if (n.kind === 'state') {
      const ini = (s: string) => s === 'init' || s.startsWith('init.')
      const pi: BayesPlanNode = { id: `${n.id}.init`, kind: 'state_init', plate, errors: 0 }
      if (n.init) {
        pi.dist = n.init.dist
        const d = catalog.distributions.find(x => x.id === n.init!.dist)
        const args: Record<string, BayesPlanSlot> = {}
        for (const a of d ? (d.params as Array<{ name: string }>).map(x => x.name) : []) {
          const s = slotOf(n.id, 'init.' + a)
          if (s) args[a] = s
        }
        pi.args = args
      }
      cands.set(pi.id, { node: pi, owner: n.id, reads: readsOf(n.id, ini), match: ownErrors(n.id, ini) })
      const ps: BayesPlanNode = { id: `${n.id}.scale`, kind: 'state_scale', plate, errors: 0 }
      const sv = slotOf(n.id, 'scale')
      if (sv) ps.value = sv
      cands.set(ps.id, { node: ps, owner: n.id, reads: readsOf(n.id, s => s === 'scale'), match: ownErrors(n.id, s => s === 'scale') })
    }
  }

  // A node is planned when everything it reads is planned (no data, no linear predictor, no state value).
  const planned = new Map<string, boolean>()
  const isPlanned = (id: string): boolean => {
    const known = planned.get(id)
    if (known !== undefined) return known
    const c = cands.get(id)
    if (!c) return false
    planned.set(id, false) // a cycle is not planned
    const ok = c.reads.every(r => cands.has(r) && isPlanned(r))
    planned.set(id, ok)
    return ok
  }
  const errorsMemo = new Map<string, number>()
  const errorCount = (id: string): number => {
    const seen = new Set<string>()
    let total = 0
    const walk = (x: string) => {
      if (seen.has(x)) return
      seen.add(x)
      const c = cands.get(x)
      if (!c) return
      total += model.findings.filter(c.match).length
      for (const r of c.reads) walk(r)
    }
    const memo = errorsMemo.get(id)
    if (memo !== undefined) return memo
    walk(id)
    errorsMemo.set(id, total)
    return total
  }
  // The parts of a correlated-effects or state node come where the node is in the order.
  for (const id of model.order) {
    const parts = [id, ...[...cands.keys()].filter(k => k !== id && ownerOf(k) === id && (cands.get(k) as Cand).owner === id)]
    for (const part of parts.sort((a, b) => cmp(a, b))) {
      const c = cands.get(part)
      if (!c || !isPlanned(part)) continue
      c.node.errors = errorCount(part)
      plan.nodes[part] = c.node
      plan.order.push(part)
    }
  }
  return plan
}
