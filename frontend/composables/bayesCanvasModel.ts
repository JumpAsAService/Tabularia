// The graph of a Bayesian model drawn on the canvas (docs/specs/bayes-graph-editor.md, «The canvas»):
// the graph is the truth, Vue Flow a drawing of it. Plates are boxes (Vue Flow parent nodes, nested by
// `parent`), nodes sit in the box of their plate, edges are the mirror's (who references whom), never
// stored. The graph's `layout` holds canvas coordinates (absolute, whatever box a node sits in); Vue
// Flow keeps a child's position relative to its parent: this module is the only code that converts
// one into the other, and its round trip is a test.
//
// Pure functions, nothing of Vue, Nuxt or the DOM: the tests import them as they are. Every export
// starts with `bayes`, `BAYES_` or `Bayes`: a composable's exports are auto-imported everywhere.
import type { BayesEdge } from '~/composables/bayesGraph'

/** The box a node is laid out in (the templates' layout check uses the same: 180 × 80). */
export const BAYES_NODE_W = 180
export const BAYES_NODE_H = 80
/** A box's header (label, key, switch) and the room left around what it holds when it has no size of its own. */
export const BAYES_PLATE_HEAD = 40
export const BAYES_PLATE_PAD = 20

export interface BayesPoint { x: number, y: number }
export interface BayesRect { x: number, y: number, w: number, h: number }

/** A plate drawn as a box: its rectangle in canvas coordinates, its depth (0 for a box in no other). */
export interface BayesBox extends BayesRect { id: string, kind: string, parent: string | null, depth: number }

/** A place of the layout: a node's top left corner, a box's with its size. */
export interface BayesPlace { x: number, y: number, w?: number, h?: number }

/** What the canvas reads of a graph document (the file's own shape: a template, a saved model). */
export interface BayesCanvasDoc {
  plates: ReadonlyArray<{ id: string, kind: string, parent?: string | null }>
  nodes: ReadonlyArray<{ id: string, kind: string, plate?: string | null, observed?: string }>
  layout: Record<string, BayesPlace>
}

/** A Vue Flow node as the canvas hands it over: a box (`plate`) or a node of the graph (`bayes`). */
export interface BayesFlowNode {
  id: string
  type: 'plate' | 'bayes'
  position: BayesPoint
  parentNode?: string
  extent?: 'parent'
  style?: { width: string, height: string }
  data: { id: string }
}

/** One drawn edge per pair of nodes: the slots between the two are one line. */
export interface BayesFlowEdge {
  id: string
  source: string
  target: string
  sourceHandle: 'r'
  targetHandle: 'l'
  /** Every kind of reference the line stands for (a group index alone is drawn dotted). */
  kinds: BayesEdge['kind'][]
}

/** The part of a node an edge's end names (`eff.ca` is a component of `eff`). */
const owner = (end: string): string => { const i = end.indexOf('.'); return i < 0 ? end : end.slice(0, i) }

/** The plate a node is drawn in: its own, or for a likelihood its observed node's (the graph spec). */
export function bayesDrawnPlate (node: BayesCanvasDoc['nodes'][number], doc: BayesCanvasDoc): string | null {
  if (node.kind === 'likelihood') {
    const observed = doc.nodes.find(n => n.id === node.observed)
    return observed?.plate ?? null
  }
  return node.plate ?? null
}

/**
 * The boxes of the document, parents before children: the rows plate and every group plate (a
 * category's levels are not a box but a badge, the graph spec). A plate with no size in the layout
 * gets the smallest box around what it holds, so a graph laid out by hand still draws.
 */
export function bayesBoxes (doc: BayesCanvasDoc): BayesBox[] {
  const plates = doc.plates.filter(p => p.kind === 'rows' || p.kind === 'group')
  const byId = new Map(plates.map(p => [p.id, p]))
  const depthOf = (id: string, seen = new Set<string>()): number => {
    const p = byId.get(id)
    if (!p?.parent || !byId.has(p.parent) || seen.has(id)) return 0
    seen.add(id)
    return 1 + depthOf(p.parent, seen)
  }
  const rect = (id: string, seen = new Set<string>()): BayesRect => {
    const at = doc.layout[id]
    if (at && at.w != null && at.h != null) return { x: at.x, y: at.y, w: at.w, h: at.h }
    seen.add(id)
    const inner: BayesRect[] = []
    for (const n of doc.nodes) {
      const place = doc.layout[n.id]
      if (place && bayesDrawnPlate(n, doc) === id) inner.push({ x: place.x, y: place.y, w: BAYES_NODE_W, h: BAYES_NODE_H })
    }
    for (const p of plates) if (p.parent === id && !seen.has(p.id)) inner.push(rect(p.id, seen))
    if (!inner.length) return { x: at?.x ?? 0, y: at?.y ?? 0, w: BAYES_NODE_W + 2 * BAYES_PLATE_PAD, h: BAYES_PLATE_HEAD + BAYES_PLATE_PAD }
    const x0 = Math.min(...inner.map(r => r.x)) - BAYES_PLATE_PAD
    const y0 = Math.min(...inner.map(r => r.y)) - BAYES_PLATE_HEAD
    const x1 = Math.max(...inner.map(r => r.x + r.w)) + BAYES_PLATE_PAD
    const y1 = Math.max(...inner.map(r => r.y + r.h)) + BAYES_PLATE_PAD
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
  }
  return plates
    .map(p => ({ id: p.id, kind: p.kind, parent: p.parent && byId.has(p.parent) ? p.parent : null, depth: depthOf(p.id), ...rect(p.id) }))
    .sort((a, b) => a.depth - b.depth)
}

/** Whether a point is inside a rectangle (its edge included). */
export function bayesInside (p: BayesPoint, r: BayesRect): boolean {
  return p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h
}

/**
 * The box a node belongs to after a drag: the deepest box under its centre, null outside every
 * box. The graph's `plate` follows; the mirror then says whether the kind may live there. Nothing
 * is moved back: the editor never repairs what the person drew.
 */
export function bayesDropPlate (centre: BayesPoint, boxes: readonly BayesBox[]): string | null {
  let best: BayesBox | null = null
  for (const b of boxes) {
    if (!bayesInside(centre, b)) continue
    if (!best || b.depth > best.depth) best = b
  }
  return best?.id ?? null
}

/**
 * The document as Vue Flow draws it: the boxes first (parents before children, as Vue Flow asks),
 * then the nodes, each placed relative to the box it sits in. A node whose plate has no box (a
 * category's levels, a plate that does not exist) stands on the canvas itself.
 */
export function bayesCanvasNodes (doc: BayesCanvasDoc): BayesFlowNode[] {
  const boxes = bayesBoxes(doc)
  const box = new Map(boxes.map(b => [b.id, b]))
  const out: BayesFlowNode[] = []
  for (const b of boxes) {
    const parent = b.parent ? box.get(b.parent) : undefined
    out.push({
      id: b.id,
      type: 'plate',
      position: parent ? { x: b.x - parent.x, y: b.y - parent.y } : { x: b.x, y: b.y },
      ...(parent ? { parentNode: parent.id, extent: 'parent' as const } : {}),
      style: { width: `${b.w}px`, height: `${b.h}px` },
      data: { id: b.id },
    })
  }
  for (const n of doc.nodes) {
    const at = doc.layout[n.id] ?? { x: 0, y: 0 }
    const plate = bayesDrawnPlate(n, doc)
    const parent = plate ? box.get(plate) : undefined
    out.push({
      id: n.id,
      type: 'bayes',
      position: parent ? { x: at.x - parent.x, y: at.y - parent.y } : { x: at.x, y: at.y },
      ...(parent ? { parentNode: parent.id, extent: 'parent' as const } : {}),
      data: { id: n.id },
    })
  }
  return out
}

/**
 * The layout of the canvas back in canvas coordinates: every node's and box's absolute place, a
 * box with its size. The inverse of `bayesCanvasNodes` for the places it drew.
 */
export function bayesCanvasLayout (nodes: readonly BayesFlowNode[]): Record<string, BayesPlace> {
  const byId = new Map(nodes.map(n => [n.id, n]))
  const absolute = (n: BayesFlowNode, seen = new Set<string>()): BayesPoint => {
    const parent = n.parentNode ? byId.get(n.parentNode) : undefined
    if (!parent || seen.has(n.id)) return { ...n.position }
    seen.add(n.id)
    const p = absolute(parent, seen)
    return { x: p.x + n.position.x, y: p.y + n.position.y }
  }
  const out: Record<string, BayesPlace> = {}
  for (const n of nodes) {
    const at = absolute(n)
    if (n.type === 'plate' && n.style) out[n.id] = { ...at, w: parseFloat(n.style.width), h: parseFloat(n.style.height) }
    else out[n.id] = at
  }
  return out
}

/** The mirror's edges as lines: one per pair of nodes (a component's edges belong to its node), none from a node to itself. */
export function bayesCanvasEdges (edges: readonly BayesEdge[]): BayesFlowEdge[] {
  const lines = new Map<string, BayesFlowEdge>()
  for (const e of edges) {
    const source = owner(e.from)
    const target = owner(e.to)
    if (source === target) continue
    const id = `${source}>${target}`
    const line = lines.get(id)
    if (line) {
      if (!line.kinds.includes(e.kind)) line.kinds.push(e.kind)
    } else {
      lines.set(id, { id, source, target, sourceHandle: 'r', targetHandle: 'l', kinds: [e.kind] })
    }
  }
  return [...lines.values()]
}

export type BayesDirection = 'left' | 'right' | 'up' | 'down'

/**
 * The node Alt with an arrow walks to: among the nodes whose centre lies in that direction (within
 * the 90° cone around it), the nearest, the distance across the direction counting twice. Null when
 * none lies there.
 */
export function bayesNearestNode (from: BayesRect, others: ReadonlyArray<{ id: string, rect: BayesRect }>, dir: BayesDirection): string | null {
  const cx = from.x + from.w / 2
  const cy = from.y + from.h / 2
  let best: { id: string, score: number } | null = null
  for (const o of others) {
    const dx = o.rect.x + o.rect.w / 2 - cx
    const dy = o.rect.y + o.rect.h / 2 - cy
    const along = dir === 'right' ? dx : dir === 'left' ? -dx : dir === 'down' ? dy : -dy
    const across = dir === 'left' || dir === 'right' ? Math.abs(dy) : Math.abs(dx)
    if (along <= 0 || across > along) continue
    const score = along + 2 * across
    if (!best || score < best.score) best = { id: o.id, score }
  }
  return best?.id ?? null
}
