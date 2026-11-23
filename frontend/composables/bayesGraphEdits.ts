// What drawing does to the graph document of a Bayesian model (docs/specs/bayes-graph-editor.md,
// «The canvas», «The palette»): a node or a box added, removed, duplicated, moved into a box or out of
// it, a box resized or nested, a slot written by an edge drawn by hand or cleared by an edge removed.
// The document is the truth and these functions change it in place; nothing here repairs what the
// person drew: a kind the graph does not allow somewhere is still put there, and the mirror's finding
// says why (the graph spec: «findings reported, never repaired»).
//
// The layout holds canvas coordinates (absolute): moving a box shifts what it holds by the same
// amount, so the drawing stays as it was seen.
//
// Pure functions, nothing of Vue, Nuxt or the DOM: the tests import them as they are. Every export
// starts with `bayes`, `BAYES_` or `Bayes`: a composable's exports are auto-imported everywhere.
import { BAYES_NODE_H, BAYES_NODE_W, BAYES_PLATE_HEAD, BAYES_PLATE_PAD, bayesBoxes, bayesDrawnPlate, bayesInside, type BayesBox, type BayesPoint, type BayesRect } from '~/composables/bayesCanvasModel'
import { bayesNewId, bayesTakenNames, bayesUniqueName } from '~/composables/bayesNames'
import { bayesArgSymbol } from '~/composables/bayesEditorText'
import { bayesDistribution } from '~/composables/bayesCatalog'
import { bayesGraphDecode, bayesGraphModel, type BayesEdge, type BayesGraphFinding } from '~/composables/bayesGraph'
import type { BayesTemplateGraph, BayesTemplateNode, BayesTemplateSlot } from '~/composables/bayesTemplates'

/** The graph document as the editor holds it (a template's shape, a saved model's). */
export type BayesDoc = BayesTemplateGraph

/** What the palette adds: a kind of the graph, a data node by its type, or a group box. */
export type BayesPaletteKind =
  | 'real' | 'int' | 'group_index' | 'category' | 'time'
  | 'parameter' | 'correlated_effects' | 'state'
  | 'transformed' | 'linear_predictor'
  | 'likelihood'
  | 'group'

/** The palette's groups and their items, in order (the spec's «The palette»). */
export const BAYES_PALETTE: ReadonlyArray<{ group: 'data' | 'unknowns' | 'values' | 'likelihood' | 'boxes', items: readonly BayesPaletteKind[] }> = [
  { group: 'data', items: ['real', 'int', 'group_index', 'category', 'time'] },
  { group: 'unknowns', items: ['parameter', 'correlated_effects', 'state'] },
  { group: 'values', items: ['transformed', 'linear_predictor'] },
  { group: 'likelihood', items: ['likelihood'] },
  { group: 'boxes', items: ['group'] },
]

/** A new box's size: room for two nodes side by side under its header. */
export const BAYES_NEW_BOX_W = 2 * BAYES_NODE_W + 3 * BAYES_PLATE_PAD
export const BAYES_NEW_BOX_H = BAYES_PLATE_HEAD + BAYES_NODE_H + 2 * BAYES_PLATE_PAD

/** The words a new item takes, in the person's language. */
export interface BayesNewWords {
  /** The new node's or box's label (the palette item's name). */
  label: string
  /** A correlated effects node: the labels of its two components and of its correlation. */
  components?: [string, string]
  correlation?: string
}

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T
const round = (v: number): number => Math.round(v)
const owner = (id: string): string => { const i = id.indexOf('.'); return i < 0 ? id : id.slice(0, i) }
const slotText = (s: BayesTemplateSlot | undefined | null): string => (s?.expr ?? s?.ref ?? '').trim()

/** Whether this id is a box (a rows or group plate) of the document. */
export function bayesIsBox (doc: BayesDoc, id: string): boolean {
  return doc.plates.some(p => p.id === id && (p.kind === 'rows' || p.kind === 'group'))
}

/** A box and every box nested in it, at any depth. */
export function bayesBoxSubtree (doc: BayesDoc, id: string): Set<string> {
  const out = new Set<string>([id])
  let grew = true
  while (grew) {
    grew = false
    for (const p of doc.plates) {
      if (p.kind === 'group' && typeof p.parent === 'string' && out.has(p.parent) && !out.has(p.id)) { out.add(p.id); grew = true }
    }
  }
  return out
}

// ---------------------------------------------------------------- adding

/**
 * Adds a node or a box of this kind at `at` (its top left corner, canvas coordinates), in `plate`
 * (a box's id, or null for none), and returns its id. Its name is proposed from its label and made
 * unique; its slots are empty or hold the catalogue's plainest form (a prior `normal(0, 1)`), and
 * the mirror says what is still to write.
 */
export function bayesAddItem (doc: BayesDoc, kind: BayesPaletteKind, at: BayesPoint, plate: string | null, words: BayesNewWords): string {
  const place = { x: round(at.x), y: round(at.y) }
  if (kind === 'group') {
    const id = bayesNewId(doc, 'p')
    const parent = plate && doc.plates.some(p => p.id === plate && p.kind === 'group') ? plate : null
    doc.plates.push({ id, kind: 'group', label: words.label, parent, key: '', form: 'non_centred' })
    doc.layout[id] = { ...place, w: BAYES_NEW_BOX_W, h: BAYES_NEW_BOX_H }
    return id
  }
  const id = bayesNewId(doc, 'n')
  const taken = bayesTakenNames(doc)
  const name = bayesUniqueName(words.label, taken)
  const empty = (): BayesTemplateSlot => ({})
  const normal = () => ({ dist: 'normal', args: { mu: { expr: '0' }, sigma: { expr: '1' } } })
  let node: BayesTemplateNode
  switch (kind) {
    case 'real': case 'int': case 'group_index': case 'category': case 'time':
      node = { id, kind: 'data', name, label: words.label, plate, type: kind }
      if (kind === 'int') node.support = 'any'
      // a real predictor is standardized by default (answer 6); the flow node centres and scales it
      if (kind === 'real') node.standardize = true
      // a group index names the box its values tell apart: the selected one, when it is a group box
      if (kind === 'group_index') node.target = ''
      break
    case 'parameter':
      node = { id, kind: 'parameter', name, label: words.label, plate, prior: normal() }
      break
    case 'state':
      node = { id, kind: 'state', name, label: words.label, plate, process: 'random_walk', init: normal(), scale: empty(), form: 'non_centred' }
      break
    case 'transformed':
      node = { id, kind: 'transformed', name, label: words.label, plate, value: empty() }
      break
    case 'linear_predictor':
      node = { id, kind: 'linear_predictor', name, label: words.label, plate, intercept: empty(), terms: [] }
      break
    case 'likelihood': {
      // no name: a likelihood is never referenced; its box is its observed data's
      const d = bayesDistribution('normal')
      const args: Record<string, BayesTemplateSlot> = {}
      for (const p of d?.params ?? [{ name: 'mu' }, { name: 'sigma' }]) args[p.name] = empty()
      node = { id, kind: 'likelihood', label: words.label, observed: '', dist: 'normal', args }
      break
    }
    case 'correlated_effects': {
      const [l1, l2] = words.components ?? ['1', '2']
      const n1 = bayesUniqueName(l1, new Set([...taken, name]))
      const n2 = bayesUniqueName(l2, new Set([...taken, name, n1]))
      const cl = words.correlation ?? words.label
      const corr = bayesUniqueName(cl, new Set([...taken, name, n1, n2]))
      // the node's own name is not used by the graph (its components are what expressions read)
      node = {
        id, kind: 'correlated_effects', label: words.label, plate,
        components: [
          { id: 'c1', name: n1, label: l1, mean: { expr: '0' }, scale: { expr: '1' } },
          { id: 'c2', name: n2, label: l2, mean: { expr: '0' }, scale: { expr: '1' } },
        ],
        correlation: { name: corr, label: cl, eta: { expr: '2' } },
      }
      break
    }
  }
  if (node.plate === null) delete node.plate
  doc.nodes.push(node)
  doc.layout[id] = place
  return id
}

/**
 * A free place for a node, near `start`: the first spot of a grid going right then down that
 * overlaps no node. Inside a box the grid starts under its header and stays within its width;
 * outside one, no spot straddles a box's edge (a node is drawn wholly in a box or wholly out of it).
 */
export function bayesFreeSpot (doc: BayesDoc, start: BayesPoint, within: BayesRect | null): BayesPoint {
  const rects: BayesRect[] = doc.nodes.flatMap(n => (doc.layout[n.id] ? [{ ...doc.layout[n.id]!, w: BAYES_NODE_W, h: BAYES_NODE_H }] : []))
  const meets = (r: BayesRect, o: BayesRect) => r.x < o.x + o.w && o.x < r.x + r.w && r.y < o.y + o.h && o.y < r.y + r.h
  const holds = (o: BayesRect, r: BayesRect) => r.x >= o.x && r.y >= o.y + BAYES_PLATE_HEAD && r.x + r.w <= o.x + o.w && r.y + r.h <= o.y + o.h
  const boxes = within ? [] : bayesBoxes(doc)
  const overlaps = (r: BayesRect) => rects.some(o => meets(r, o)) || boxes.some(b => meets(r, b) && !holds(b, r))
  const stepX = BAYES_NODE_W + BAYES_PLATE_PAD
  const stepY = BAYES_NODE_H / 2 + 10
  const free = (p: BayesPoint) => !overlaps({ ...p, w: BAYES_NODE_W, h: BAYES_NODE_H })
  if (within) {
    const x0 = within.x + BAYES_PLATE_PAD
    const y0 = within.y + BAYES_PLATE_HEAD
    const cols = Math.max(1, Math.floor((within.w - BAYES_PLATE_PAD) / stepX))
    for (let i = 0; i < 200; i++) {
      const p = { x: x0 + (i % cols) * stepX, y: y0 + Math.floor(i / cols) * stepY }
      if (free(p)) return { x: round(p.x), y: round(p.y) }
    }
    return { x: round(x0), y: round(y0) }
  }
  // on the canvas: the free spot nearest to `start`, so that it stays in sight
  const spots: Array<BayesPoint & { d: number }> = []
  for (let i = -8; i <= 8; i++) {
    for (let j = -16; j <= 16; j++) {
      const p = { x: start.x + i * stepX / 2, y: start.y + j * stepY / 2 }
      spots.push({ ...p, d: Math.hypot(p.x - start.x, (p.y - start.y) * 1.5) })
    }
  }
  spots.sort((a, b) => a.d - b.d)
  const p = spots.find(free) ?? start
  return { x: round(p.x), y: round(p.y) }
}

// ---------------------------------------------------------------- removing, duplicating

export type BayesRemoveResult = { ok: true } | { ok: false, reason: 'rows' | 'unknown' }

/**
 * Removes a node or a box. A box's nodes and boxes move to the box it sat in (or to none): what was
 * drawn inside it stays where it was seen. The rows box is part of every model and stays. References
 * to a removed node stay in the expressions that hold them: the mirror names each.
 */
export function bayesRemoveItem (doc: BayesDoc, id: string): BayesRemoveResult {
  const plate = doc.plates.find(p => p.id === id)
  if (plate) {
    if (plate.kind === 'rows') return { ok: false, reason: 'rows' }
    const up = typeof plate.parent === 'string' ? plate.parent : null
    for (const p of doc.plates) if (p.kind === 'group' && p.parent === id) p.parent = up
    for (const n of doc.nodes) {
      if (n.plate !== id) continue
      if (up) n.plate = up
      else delete n.plate
    }
    doc.plates.splice(doc.plates.indexOf(plate), 1)
    delete doc.layout[id]
    return { ok: true }
  }
  const i = doc.nodes.findIndex(n => n.id === id)
  if (i < 0) return { ok: false, reason: 'unknown' }
  doc.nodes.splice(i, 1)
  delete doc.layout[id]
  return { ok: true }
}

/** A copy of a node beside it, in the same box, its names made unique; null for a box or an unknown id. */
export function bayesDuplicateNode (doc: BayesDoc, id: string): string | null {
  const n = doc.nodes.find(x => x.id === id)
  if (!n) return null
  const copy = clone(n)
  copy.id = bayesNewId(doc, 'n')
  const taken = bayesTakenNames(doc)
  const unique = (name: string) => { const u = bayesUniqueName(name, taken); taken.add(u); return u }
  if (typeof copy.name === 'string' && copy.name) copy.name = unique(copy.name)
  for (const c of copy.components ?? []) c.name = unique(c.name)
  if (copy.correlation) copy.correlation.name = unique(copy.correlation.name)
  doc.nodes.push(copy)
  const at = doc.layout[id] ?? { x: 0, y: 0 }
  doc.layout[copy.id] = { x: at.x + 32, y: at.y + 32 }
  return copy.id
}

// ---------------------------------------------------------------- moving, nesting, resizing

/**
 * A node or a box moved to `to` (its top left corner, canvas coordinates). A box takes what it
 * holds along (its nodes and its boxes, at any depth); a box with no size of its own gets the size
 * it was drawn with.
 */
export function bayesMoveItem (doc: BayesDoc, id: string, to: BayesPoint): void {
  const target = { x: round(to.x), y: round(to.y) }
  if (!bayesIsBox(doc, id)) {
    if (doc.layout[id] || doc.nodes.some(n => n.id === id)) doc.layout[id] = target
    return
  }
  const box = bayesBoxes(doc).find(b => b.id === id)
  if (!box) return
  const dx = target.x - box.x
  const dy = target.y - box.y
  doc.layout[id] = { x: target.x, y: target.y, w: box.w, h: box.h }
  if (dx === 0 && dy === 0) return
  const inside = bayesBoxSubtree(doc, id)
  for (const p of doc.plates) {
    if (p.id === id || !inside.has(p.id)) continue
    const at = doc.layout[p.id]
    if (at) doc.layout[p.id] = { ...at, x: at.x + dx, y: at.y + dy }
  }
  for (const n of doc.nodes) {
    const plate = bayesDrawnPlate(n, doc)
    const at = doc.layout[n.id]
    if (plate && inside.has(plate) && at) doc.layout[n.id] = { x: at.x + dx, y: at.y + dy }
  }
}

/** A box's new rectangle after a resize (canvas coordinates); what it holds stays where it is. */
export function bayesResizeBox (doc: BayesDoc, id: string, r: BayesRect): void {
  if (!bayesIsBox(doc, id)) return
  doc.layout[id] = { x: round(r.x), y: round(r.y), w: Math.max(1, round(r.w)), h: Math.max(1, round(r.h)) }
}

/** The box a node is in: its plate, or none. A likelihood's box is its observed data's and does not change. */
export function bayesSetPlate (doc: BayesDoc, id: string, plate: string | null): boolean {
  const n = doc.nodes.find(x => x.id === id)
  if (!n || n.kind === 'likelihood') return false
  if ((n.plate ?? null) === plate) return false
  if (plate) n.plate = plate
  else delete n.plate
  return true
}

/** The group box a group box sits in, or none. Only a group box nests; a box never goes into itself. */
export function bayesSetParent (doc: BayesDoc, id: string, parent: string | null): boolean {
  const p = doc.plates.find(x => x.id === id)
  if (!p || p.kind !== 'group') return false
  if (parent !== null && bayesBoxSubtree(doc, id).has(parent)) return false
  if ((p.parent ?? null) === parent) return false
  p.parent = parent
  return true
}

/**
 * Where a dragged item lands. A node: the deepest box under its centre (the canvas outside every box
 * is a scalar). A box: the deepest group box under the middle of its header, never itself nor a box
 * it holds (the rows box holds no box: the rows and the groups are linked by their key data nodes).
 */
export function bayesDropTarget (doc: BayesDoc, id: string, rect: BayesRect): string | null {
  const boxes = bayesBoxes(doc)
  if (bayesIsBox(doc, id)) {
    const own = bayesBoxSubtree(doc, id)
    const point = { x: rect.x + rect.w / 2, y: rect.y + BAYES_PLATE_HEAD / 2 }
    return deepest(point, boxes.filter(b => b.kind === 'group' && !own.has(b.id)))
  }
  return deepest({ x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 }, boxes)
}

function deepest (p: BayesPoint, boxes: readonly BayesBox[]): string | null {
  let best: BayesBox | null = null
  for (const b of boxes) if (bayesInside(p, b) && (!best || b.depth > best.depth)) best = b
  return best?.id ?? null
}

/**
 * A drag that stopped: the item at its new place, then in the box it was dropped in. Returns
 * whether its box changed.
 */
export function bayesDropItem (doc: BayesDoc, id: string, rect: BayesRect): boolean {
  bayesMoveItem(doc, id, rect)
  const target = bayesDropTarget(doc, id, rect)
  return bayesIsBox(doc, id) ? bayesSetParent(doc, id, target) : bayesSetPlate(doc, id, target)
}

// ---------------------------------------------------------------- boxes' own settings

/** A group box's form: non-centred (the default) or centred. */
export function bayesSetForm (doc: BayesDoc, id: string, nonCentred: boolean): void {
  const p = doc.plates.find(x => x.id === id)
  if (p && p.kind === 'group') p.form = nonCentred ? 'non_centred' : 'centred'
}

/**
 * The rows box's switch «these rows are a time series»: on, the series takes the first time node of
 * the rows (or none yet: the mirror says it is missing) and no unit; off, it has no series.
 */
export function bayesSetSeries (doc: BayesDoc, on: boolean): void {
  const rows = doc.plates.find(p => p.kind === 'rows')
  if (!rows) return
  if (!on) { delete rows.series; return }
  if (rows.series) return
  const time = doc.nodes.find(n => n.kind === 'data' && n.type === 'time' && n.plate === rows.id)
  rows.series = { time: time?.id ?? '', by: null }
}

/** The series' time node or its unit (a group box, or null for a single series). */
export function bayesSetSeriesPart (doc: BayesDoc, part: 'time' | 'by', value: string | null): void {
  const rows = doc.plates.find(p => p.kind === 'rows')
  const s = rows?.series as { time: string, by: string | null } | undefined
  if (!s) return
  if (part === 'time') s.time = value ?? ''
  else s.by = value
}

// ---------------------------------------------------------------- slots and edges

/** A slot of a node an edge can write: where it is in the document and how the mirror names it. */
export interface BayesSlotPlace {
  /** The path in the node: `prior.mu`, `value`, `terms[1].coef`, `terms.new`, `observed`, `c1.mean`, `correlation.eta`. */
  path: string
  /** How the mirror's findings name it: the node (or `node.component`) and the slot. */
  node: string
  slot: string
  /** It takes a data node's id (observed, a term's data), not an expression. */
  data: boolean
  /** Its words: an i18n key under `bayes.editor.slot` and its placeholders. */
  words: { key: string, params: Record<string, string> }
  /** What it holds now ('' when empty). */
  text: string
}

/** Every slot of a node an edge can write, in the order the inspector shows them. */
export function bayesNodeSlots (doc: BayesDoc, id: string): BayesSlotPlace[] {
  const n = doc.nodes.find(x => x.id === id)
  if (!n) return []
  const out: BayesSlotPlace[] = []
  const nameOf = (nid: string) => String(doc.nodes.find(x => x.id === nid)?.name ?? nid)
  const dist = (prefix: 'prior' | 'init' | 'args', d: string, args: Record<string, BayesTemplateSlot> | undefined) => {
    const order = bayesDistribution(d)?.params.map(p => p.name) ?? Object.keys(args ?? {})
    for (const a of order) out.push({ path: `${prefix}.${a}`, node: n.id, slot: `${prefix}.${a}`, data: false, words: { key: prefix, params: { arg: bayesArgSymbol(a) } }, text: slotText(args?.[a]) })
  }
  const one = (path: string, key: string, s: unknown, params: Record<string, string> = {}) =>
    out.push({ path, node: n.id, slot: path, data: false, words: { key, params }, text: slotText(s as BayesTemplateSlot) })
  switch (n.kind) {
    case 'parameter':
      if (n.prior) dist('prior', n.prior.dist, n.prior.args)
      break
    case 'transformed':
      one('value', 'value', n.value)
      break
    case 'linear_predictor': {
      one('intercept', 'intercept', n.intercept)
      const terms = (n.terms ?? []) as Array<{ data: string, coef: BayesTemplateSlot }>
      terms.forEach((t, i) => {
        one(`terms[${i}].coef`, 'coef', t.coef, { data: nameOf(t.data) })
        out.push({ path: `terms[${i}].data`, node: n.id, slot: `terms[${i}].data`, data: true, words: { key: 'termData', params: { coef: slotText(t.coef) || '…' } }, text: t.data ? nameOf(t.data) : '' })
      })
      out.push({ path: 'terms.new', node: n.id, slot: `terms[${terms.length}].data`, data: true, words: { key: 'newTerm', params: {} }, text: '' })
      break
    }
    case 'likelihood':
      out.push({ path: 'observed', node: n.id, slot: 'observed', data: true, words: { key: 'observed', params: {} }, text: n.observed ? nameOf(String(n.observed)) : '' })
      dist('args', String(n.dist), n.args)
      break
    case 'state':
      if (n.init) dist('init', n.init.dist, n.init.args)
      one('scale', 'scale', n.scale)
      break
    case 'correlated_effects':
      for (const c of n.components ?? []) {
        out.push({ path: `${c.id}.mean`, node: `${n.id}.${c.id}`, slot: 'mean', data: false, words: { key: 'compMean', params: { name: c.name } }, text: slotText(c.mean) })
        out.push({ path: `${c.id}.scale`, node: `${n.id}.${c.id}`, slot: 'scale', data: false, words: { key: 'compScale', params: { name: c.name } }, text: slotText(c.scale) })
      }
      if (n.correlation) out.push({ path: 'correlation.eta', node: `${n.id}.correlation`, slot: 'eta', data: false, words: { key: 'eta', params: {} }, text: slotText(n.correlation.eta) })
      break
  }
  return out
}

/** Writes a slot of a node by its path: an expression slot takes `{expr}` or `{ref}`, a data slot an id ('' clears it). */
export function bayesWriteSlot (doc: BayesDoc, id: string, path: string, value: BayesTemplateSlot | string): boolean {
  const n = doc.nodes.find(x => x.id === id)
  if (!n) return false
  const slot = (): BayesTemplateSlot => (typeof value === 'string' ? { expr: value } : clone(value))
  const dataId = typeof value === 'string' ? value : (value.ref ?? value.expr ?? '')
  let m: RegExpMatchArray | null
  if ((m = path.match(/^(prior|init|args)\.(\w+)$/))) {
    const holder = m[1] === 'args' ? (n.args ??= {}) : (m[1] === 'prior' ? n.prior : n.init)?.args
    if (!holder) return false
    holder[m[2]!] = slot()
    return true
  }
  if (path === 'value' || path === 'intercept' || path === 'scale') { n[path] = slot(); return true }
  const terms = (n.terms ??= []) as Array<{ data: string, coef: BayesTemplateSlot }>
  if (path === 'terms.new') { terms.push({ data: dataId, coef: {} }); return true }
  if ((m = path.match(/^terms\[(\d+)\]\.(coef|data)$/))) {
    const t = terms[Number(m[1])]
    if (!t) return false
    if (m[2] === 'coef') t.coef = slot()
    else t.data = dataId
    return true
  }
  if (path === 'observed') { n.observed = dataId; return true }
  if ((m = path.match(/^correlation\.eta$/)) && n.correlation) { n.correlation.eta = slot(); return true }
  if ((m = path.match(/^(\w+)\.(mean|scale)$/))) {
    const c = n.components?.find(x => x.id === m![1])
    if (!c) return false
    c[m[2] as 'mean' | 'scale'] = slot()
    return true
  }
  return false
}

/** One way an edge drawn from a source to a target can be written: this slot gets this value. */
export interface BayesEdgeChoice {
  slot: BayesSlotPlace
  /** What the slot gets: the source's name, a component's name, a `ref` to a nameless source, or a data node's id. */
  value: BayesTemplateSlot | string
  /** The value as the person reads it. */
  shown: string
}

function mirror (doc: BayesDoc): BayesGraphFinding[] {
  const d = bayesGraphDecode(JSON.stringify(doc))
  return d.graph ? bayesGraphModel(d.graph).findings : d.findings
}

/**
 * The slots of `target` that can take `source` (the spec's «a small menu lists the target's slots
 * that can take the source»): each is tried in a copy of the graph, and the mirror decides — a choice
 * is offered when it puts no error on that slot and makes no cycle. A slot that holds that value
 * already is left out.
 */
export function bayesEdgeChoices (doc: BayesDoc, source: string, target: string): BayesEdgeChoice[] {
  const src = doc.nodes.find(n => n.id === source)
  if (!src || source === target || src.kind === 'likelihood') return []
  const values: Array<{ value: BayesTemplateSlot, shown: string }> = []
  if (src.kind === 'correlated_effects') {
    for (const c of src.components ?? []) if (c.name) values.push({ value: { expr: c.name }, shown: c.name })
  } else if (typeof src.name === 'string' && src.name) {
    values.push({ value: { expr: src.name }, shown: src.name })
  } else {
    values.push({ value: { ref: src.id }, shown: src.label })
  }
  const cycles = mirror(doc).filter(f => f.code === 'graph.cycle').length
  const out: BayesEdgeChoice[] = []
  for (const slot of bayesNodeSlots(doc, target)) {
    const tries: Array<{ value: BayesTemplateSlot | string, shown: string }> = slot.data
      ? (src.kind === 'data' ? [{ value: src.id, shown: String(src.name ?? src.id) }] : [])
      : values
    for (const v of tries) {
      if (slot.text && slot.text === (typeof v.value === 'string' ? String(src.name ?? src.id) : (v.value.expr ?? ''))) continue
      const trial = clone(doc)
      bayesWriteSlot(trial, target, slot.path, v.value)
      const fs = mirror(trial)
      const bad = fs.some(f => f.severity === 'error' && f.node === slot.node && f.slot === slot.slot)
      const looped = fs.filter(f => f.code === 'graph.cycle').length > cycles
      if (!bad && !looped) out.push({ slot, value: v.value, shown: v.shown })
    }
  }
  return out
}

export type BayesDisconnectResult =
  | { ok: true }
  | { ok: false, node: string, slot: BayesSlotPlace, name: string }

/**
 * Removes the edge from `source` to `target`: every slot of the target that held only that reference
 * is cleared (a term whose data it was goes, coefficient and all; an observed node is left empty).
 * When a slot's expression holds more than the reference, nothing changes: the person edits the
 * expression (the spec: «edit the expression of `mu` to remove `tau`»).
 */
export function bayesDisconnect (doc: BayesDoc, edges: readonly BayesEdge[], source: string, target: string): BayesDisconnectResult {
  const src = doc.nodes.find(n => n.id === source)
  const slots = bayesNodeSlots(doc, target)
  const mine = edges.filter(e => owner(e.from) === source && e.to === target)
  const nameOfFrom = (from: string): string => {
    if (!src) return from
    const dot = from.indexOf('.')
    if (dot >= 0) return src.components?.find(c => c.id === from.slice(dot + 1))?.name ?? from
    return String(src.name ?? '')
  }
  const clears: BayesSlotPlace[] = []
  const termsGone = new Set<number>()
  for (const e of mine) {
    const slot = slots.find(s => s.node === e.node && s.slot === e.slot)
    if (!slot) continue
    if (e.kind === 'term') {
      const m = slot.path.match(/^terms\[(\d+)\]\.data$/)
      if (m) termsGone.add(Number(m[1]))
      continue
    }
    if (e.kind === 'observed') { clears.push(slot); continue }
    const t = doc.nodes.find(n => n.id === target)
    const raw = rawSlot(t, slot.path)
    const name = nameOfFrom(e.from)
    const alone = (raw?.ref != null && raw.ref === source) || (raw?.expr != null && raw.expr.trim() === name && name !== '')
    if (!alone) return { ok: false, node: target, slot, name: name || (src?.label ?? source) }
    if (!clears.includes(slot)) clears.push(slot)
  }
  for (const s of clears) {
    if (/^terms\[(\d+)\]\.coef$/.test(s.path) && termsGone.has(Number(s.path.match(/\d+/)![0]))) continue
    bayesWriteSlot(doc, target, s.path, s.data ? '' : {})
  }
  const t = doc.nodes.find(n => n.id === target)
  const terms = t?.terms as unknown[] | undefined
  if (terms) for (const i of [...termsGone].sort((a, b) => b - a)) terms.splice(i, 1)
  return { ok: true }
}

function rawSlot (n: BayesTemplateNode | undefined, path: string): BayesTemplateSlot | undefined {
  if (!n) return undefined
  let m: RegExpMatchArray | null
  if ((m = path.match(/^(prior|init|args)\.(\w+)$/))) return (m[1] === 'args' ? n.args : (m[1] === 'prior' ? n.prior : n.init)?.args)?.[m[2]!]
  if (path === 'value' || path === 'intercept' || path === 'scale') return n[path] as BayesTemplateSlot | undefined
  if ((m = path.match(/^terms\[(\d+)\]\.coef$/))) return ((n.terms ?? []) as Array<{ coef: BayesTemplateSlot }>)[Number(m[1])]?.coef
  if (path === 'correlation.eta') return n.correlation?.eta
  if ((m = path.match(/^(\w+)\.(mean|scale)$/))) return n.components?.find(c => c.id === m![1])?.[m[2] as 'mean' | 'scale']
  return undefined
}

// ---------------------------------------------------------------- labels and names

/**
 * A node's or a box's label. While a node's name is still the one proposed from its label, it
 * follows the new label; a name the person wrote stays.
 */
export function bayesSetLabel (doc: BayesDoc, id: string, label: string, fallback = 'node'): void {
  const p = doc.plates.find(x => x.id === id)
  if (p) { p.label = label; return }
  const n = doc.nodes.find(x => x.id === id)
  if (!n) return
  if (typeof n.name === 'string' && n.kind !== 'likelihood' && n.kind !== 'correlated_effects') {
    const taken = bayesTakenNames(doc, id)
    if (n.name === bayesUniqueName(n.label, taken, fallback)) n.name = bayesUniqueName(label, taken, fallback)
  }
  n.label = label
}

/** A node's name as the person typed it; '' removes it (a node may have none). */
export function bayesSetName (doc: BayesDoc, id: string, name: string): void {
  const n = doc.nodes.find(x => x.id === id)
  if (!n) return
  if (name === '') delete n.name
  else n.name = name
}

/** A group box's key: the group index data node whose values tell its units apart. */
export function bayesSetKey (doc: BayesDoc, id: string, key: string): void {
  const p = doc.plates.find(x => x.id === id)
  if (p && p.kind === 'group') p.key = key
}

/** A group index's target box. */
export function bayesSetTarget (doc: BayesDoc, id: string, target: string): void {
  const n = doc.nodes.find(x => x.id === id)
  if (n && n.kind === 'data' && n.type === 'group_index') n.target = target
}
