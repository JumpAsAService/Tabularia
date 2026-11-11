// The flow as a chain of steps, for the gestures that treat it as one: adding a step (it goes
// INTO the chain when the node already feeds one, instead of always starting a branch),
// walking the graph from the keyboard, knowing which edges lead to a node, and where the view
// opens on a flow too long to be read whole.
//
// Pure: plain nodes and edges in, a plan out. The editor (FlowEditor.vue) carries the plan
// out on Vue Flow; nothing here asks the engine anything.

export interface ChainNode {
  id: string
  type?: string
  position: { x: number; y: number }
  parentNode?: string
  width?: number
  height?: number
}
export interface ChainEdge {
  id: string
  source: string
  target: string
  sourceHandle?: string | null
  targetHandle?: string | null
}
interface Point { x: number; y: number }

/** A step's width on the canvas: one node and the gap to the next. */
export const FLOW_STEP_X = 230
// a node wider than a step (a long title, many columns named in its body) pushes the next
// one further: this much air after its right edge
const STEP_GAP = 60
// a child this much closer than a whole step to the new node still has room: nothing moves
const ROOM_SLACK = 40
// node types whose data output a transformation can read (the «out» handle)
const READABLE = new Set(['source', 'operation', 'foreach', 'script', 'container'])

const isSequence = (e: ChainEdge) => e.targetHandle === 'seq-in' || e.sourceHandle === 'seq-out'
const dataEdges = (edges: ChainEdge[]) => edges.filter((e) => !isSequence(e))

function downstreamOf(edges: ChainEdge[], from: string): Set<string> {
  const seen = new Set<string>()
  const queue = [from]
  while (queue.length) {
    const id = queue.shift()!
    if (seen.has(id)) continue
    seen.add(id)
    for (const e of edges) if (e.source === id) queue.push(e.target)
  }
  return seen
}

export interface StepPlan {
  /** where the new node stands, in its parent container's coordinates when it has one */
  position: Point
  /** the foreach container the new node belongs to */
  parentNode?: string
  removeEdgeIds: string[]
  addEdges: ChainEdge[]
  /** the nodes that make room, with the position they end at */
  moves: { id: string; x: number; y: number }[]
  /** a foreach container that widens to keep its body inside */
  resize?: { id: string; width: number }
  /** true: the step went between two nodes; false: it starts or continues a branch */
  inserted: boolean
}

export interface StepOptions {
  /** start a branch even when the node already feeds one (Shift) */
  branch?: boolean
  /** insert on this edge of the node, whatever else it feeds (a drop on the edge) */
  edgeId?: string
  /** the place the person pointed at; without it the step stands beside its parent */
  at?: Point
  /** with `at`, on an edge with no room: stand beside the parent and push the rest on */
  snap?: boolean
}

/**
 * The plan for a new step reading `parentId`.
 *
 * A node that feeds exactly ONE node takes the step between the two: the edge parent → child
 * becomes parent → new → child, the child keeps the input it was reached on (a join's left
 * or right), and what follows moves on (one step, more after a wide node) when there is no
 * room. A node that feeds
 * nothing takes it at its end. A node that feeds SEVERAL is a fork already: the step is one
 * more branch, as with `branch`; a drop on one of its edges (`edgeId`) says which.
 * Null when `parentId` has no data output to read.
 */
export function planStep(
  nodes: ChainNode[], edges: ChainEdge[], parentId: string, newId: string, opts: StepOptions = {},
): StepPlan | null {
  const parent = nodes.find((n) => n.id === parentId)
  if (!parent || !READABLE.has(parent.type ?? '')) return null
  const data = dataEdges(edges)
  const outs = data.filter((e) => e.source === parentId)
  const onEdge = opts.edgeId ? outs.find((e) => e.id === opts.edgeId) : undefined
  const edge = onEdge ?? (!opts.branch && !opts.edgeId && outs.length === 1 ? outs[0] : undefined)
  const child = edge ? nodes.find((n) => n.id === edge.target) : undefined
  const outHandle = parent.type === 'script' || parent.type === 'container' ? 'out' : undefined
  // beside the parent, clear of its right edge; on the child's row when the parent forks
  // (the parent's own row is another branch's there)
  const beside = {
    x: parent.position.x + Math.max(FLOW_STEP_X, (parent.width ?? 0) + STEP_GAP),
    y: child && outs.length > 1 ? child.position.y : parent.position.y,
  }
  const space = nodes.filter((n) => n.parentNode === parent.parentNode && n.id !== parentId)

  if (!edge || !child) {
    // the end of a chain, or one more branch: beside the node, lower while the place is taken
    const position = opts.at ?? { ...beside }
    if (!opts.at) {
      const taken = (y: number) => space.some((n) => Math.abs(n.position.x - position.x) < 180 && Math.abs(n.position.y - y) < 80)
      for (let k = 0; k < 12 && taken(position.y); k++) position.y += 90
    }
    return {
      position, parentNode: parent.parentNode, inserted: false, removeEdgeIds: [], moves: [],
      addEdges: [{ id: `e-${parentId}-left-${newId}`, source: parentId, target: newId, sourceHandle: outHandle, targetHandle: 'left' }],
    }
  }

  const handle = edge.targetHandle || 'left'
  const addEdges: ChainEdge[] = [
    { id: `e-${parentId}-left-${newId}`, source: parentId, target: newId, sourceHandle: edge.sourceHandle ?? outHandle, targetHandle: 'left' },
    { id: `e-${newId}-${handle}-${child.id}`, source: newId, target: child.id, targetHandle: edge.targetHandle ?? undefined },
  ]
  const sameSpace = child.parentNode === parent.parentNode
  // how far the child must move to stand one step after the new node
  const push = beside.x + FLOW_STEP_X - child.position.x
  const room = !sameSpace || push <= ROOM_SLACK
  // the place pointed at is kept when it was the person's choice of an empty spot, or when
  // the edge has room for a node; a drop on a tight edge stands in line and pushes the rest
  const position = opts.at && (room || !opts.snap) ? opts.at : beside
  const plan: StepPlan = { position, parentNode: parent.parentNode, inserted: true, removeEdgeIds: [edge.id], addEdges, moves: [] }
  if (room || (opts.at && !opts.snap)) return plan

  // what follows moves one step on: the child and everything downstream of it that shares
  // its coordinates (a container's body moves with the container)
  const after = downstreamOf(data, child.id)
  const moved = nodes.filter((n) => after.has(n.id) && n.parentNode === child.parentNode && n.id !== parentId)
  plan.moves = moved.map((n) => ({ id: n.id, x: n.position.x + push, y: n.position.y }))
  // inside a foreach container: it widens to keep its body, and what follows IT moves by as much
  const box = child.parentNode ? nodes.find((n) => n.id === child.parentNode) : undefined
  if (box) {
    const needed = Math.max(...plan.moves.map((m) => m.x + (nodes.find((n) => n.id === m.id)?.width || 180))) + 24
    const width = box.width || 460
    if (needed > width) {
      plan.resize = { id: box.id, width: needed }
      const grow = needed - width
      const beyond = downstreamOf(data, box.id)
      for (const n of nodes) {
        if (n.id !== box.id && beyond.has(n.id) && n.parentNode === box.parentNode) plan.moves.push({ id: n.id, x: n.position.x + grow, y: n.position.y })
      }
    }
  }
  return plan
}

export type Walk = 'upstream' | 'downstream' | 'above' | 'below'

/**
 * The node the keyboard reaches from `id`: the step before (the left input first; a foreach
 * body's first node goes to its container), the step after (the nearest in height when the
 * node forks), or the sibling above or below — for a step, a node fed by the same node or
 * feeding the same node (the two sides of a join); for a start of the flow, its other
 * starts. Null at the end of the way.
 */
export function chainNeighbour(nodes: ChainNode[], edges: ChainEdge[], id: string, way: Walk): string | null {
  const me = nodes.find((n) => n.id === id)
  if (!me) return null
  const data = dataEdges(edges)
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const absY = (n: ChainNode) => n.position.y + (n.parentNode ? byId.get(n.parentNode)?.position.y ?? 0 : 0)
  const into = data.filter((e) => e.target === id && byId.has(e.source))
  const out = data.filter((e) => e.source === id && byId.has(e.target))

  if (way === 'upstream') {
    const left = into.find((e) => (e.targetHandle || 'left') !== 'right') ?? into[0]
    return left?.source ?? me.parentNode ?? null
  }
  if (way === 'downstream') {
    const next = out.map((e) => byId.get(e.target)!)
      .sort((a, b) => Math.abs(absY(a) - absY(me)) - Math.abs(absY(b) - absY(me)) || absY(a) - absY(b))
    return next[0]?.id ?? null
  }
  const parents = new Set(into.map((e) => e.source))
  const children = new Set(out.map((e) => e.target))
  const fed = new Set(data.map((e) => e.target))
  const siblings = nodes.filter((n) => {
    if (n.id === id) return true
    if (n.type === 'comment') return false
    // the flow's starts, one under the other: among themselves only, so that down and up
    // again comes back to the same node
    if (!into.length) return !fed.has(n.id) && n.parentNode === me.parentNode && me.type !== 'comment'
    if (data.some((e) => e.target === n.id && parents.has(e.source))) return true
    return data.some((e) => e.source === n.id && children.has(e.target))
  }).sort((a, b) => absY(a) - absY(b) || a.position.x - b.position.x)
  const at = siblings.findIndex((n) => n.id === id)
  return siblings[at + (way === 'above' ? -1 : 1)]?.id ?? null
}

/**
 * The data edges that lead to `id`: every input of every node upstream (both sides of a
 * join, a foreach's driver, and for a node of a foreach body what its container reads).
 * They are the edges the engine is working along while the node's preview is computed.
 */
export function upstreamEdgeIds(nodes: ChainNode[], edges: ChainEdge[], id: string): Set<string> {
  const data = dataEdges(edges)
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const found = new Set<string>()
  const seen = new Set<string>()
  const queue = [id]
  while (queue.length) {
    const cur = queue.shift()!
    if (seen.has(cur)) continue
    seen.add(cur)
    for (const e of data) {
      if (e.target !== cur) continue
      found.add(e.id)
      queue.push(e.source)
    }
    const box = byId.get(cur)?.parentNode
    if (box) queue.push(box)
  }
  return found
}

/** Where a flow ends: its Output nodes, or without any the nodes nothing reads. */
export function flowEndIds(nodes: ChainNode[], edges: ChainEdge[]): string[] {
  const top = nodes.filter((n) => !n.parentNode && n.type !== 'comment')
  const outputs = top.filter((n) => n.type === 'output')
  if (outputs.length) return outputs.map((n) => n.id)
  const read = new Set(dataEdges(edges).map((e) => e.source))
  return top.filter((n) => !read.has(n.id)).map((n) => n.id)
}

export interface ViewRect { x: number; y: number; width: number; height: number }
export interface OpeningOptions {
  /** the zoom under which nodes are no longer read */
  floor?: number
  padding?: number
  maxZoom?: number
  /** kept free on the pane's sides (the minimap sits bottom right) */
  margin?: number
  marginRight?: number
}

function boundsOf(rects: ViewRect[]): ViewRect {
  const x = Math.min(...rects.map((r) => r.x))
  const y = Math.min(...rects.map((r) => r.y))
  return {
    x, y,
    width: Math.max(...rects.map((r) => r.x + r.width)) - x,
    height: Math.max(...rects.map((r) => r.y + r.height)) - y,
  }
}

/**
 * The viewport a flow opens with when fitting it whole would shrink it under the legible
 * zoom: `floor`, anchored to the flow's END (`ends`: its outputs), where the result is and
 * where the next step goes; the rest is in the minimap. Null when the whole flow fits at a
 * legible zoom: the ordinary fit is right. The zoom of the fit is Vue Flow's own
 * (`pane / (bounds × (1 + padding))`, the smaller of the two axes).
 */
export function openingViewport(
  all: ViewRect[], ends: ViewRect[], pane: { width: number; height: number }, opts: OpeningOptions = {},
): { x: number; y: number; zoom: number } | null {
  const floor = opts.floor ?? 0.6
  const padding = opts.padding ?? 0.15
  const margin = opts.margin ?? 48
  const marginRight = opts.marginRight ?? margin
  if (!all.length || pane.width <= 0 || pane.height <= 0) return null
  const b = boundsOf(all)
  const fit = Math.min(pane.width / (b.width * (1 + padding)), pane.height / (b.height * (1 + padding)), opts.maxZoom ?? 1)
  if (fit >= floor) return null
  const zoom = floor
  const end = boundsOf(ends.length ? ends : all)
  // an axis the flow fits on is centred whole; on the other the end is brought in, without
  // ever showing the emptiness past the flow's own edge
  const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, Math.min(lo, hi)), Math.max(lo, hi))
  const x = b.width * zoom <= pane.width - margin - marginRight
    ? (pane.width - b.width * zoom) / 2 - b.x * zoom
    : clamp(pane.width - marginRight - (end.x + end.width) * zoom, pane.width - marginRight - (b.x + b.width) * zoom, margin - b.x * zoom)
  const y = b.height * zoom <= pane.height - 2 * margin
    ? (pane.height - b.height * zoom) / 2 - b.y * zoom
    : clamp(pane.height / 2 - (end.y + end.height / 2) * zoom, pane.height - margin - (b.y + b.height) * zoom, margin - b.y * zoom)
  return { x: Math.round(x), y: Math.round(y), zoom }
}

/** `--ease-out` (cubic-bezier(0.16, 1, 0.3, 1)) as a function of time, for what moves on the
 *  canvas by its position and cannot be handed to a CSS transition. */
export function easeOutExpo(t: number): number {
  return t >= 1 ? 1 : t <= 0 ? 0 : 1 - 2 ** (-10 * t)
}
