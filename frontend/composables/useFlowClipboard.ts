// Copying and pasting the editor's nodes.
//
// What is copied is a piece of flow in the same shape it is saved in: the chosen nodes,
// the chosen containers' children, and the edges lying ALL inside the selection (an edge
// towards a node not copied would have nowhere to attach). It sits in localStorage and
// not in a variable: so one can paste into another flow or another tab too. The system
// clipboard is not used: it asks the browser for a permission, and whoever pastes into a
// document would get JSON.

export interface ClipNode {
  id: string
  type?: string
  position: { x: number; y: number }
  data: any
  parentNode?: string
  style?: Record<string, string>
}

export interface ClipEdge {
  id: string
  source: string
  target: string
  sourceHandle?: string | null
  targetHandle?: string | null
}

export interface FlowClip {
  v: 1
  nodes: ClipNode[]
  edges: ClipEdge[]
}

const STORAGE_KEY = 'tabularia.flowClipboard'
let memory: FlowClip | null = null // if localStorage is not there (private browsing, quota)

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v))

/** The piece of flow to copy. `null` when nothing is selected. */
export function buildClip(nodes: ClipNode[], edges: ClipEdge[], selected: string[]): FlowClip | null {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const chosen = new Set(selected.filter((id) => byId.has(id)))
  // a container brings its content along
  for (const n of nodes) if (n.parentNode && chosen.has(n.parentNode)) chosen.add(n.id)
  if (!chosen.size) return null

  const out: ClipNode[] = []
  for (const n of nodes) {
    if (!chosen.has(n.id)) continue
    const copy = clone(n)
    const parent = n.parentNode ? byId.get(n.parentNode) : undefined
    if (parent && !chosen.has(parent.id)) {
      // a child copied without its container: it becomes a free node, at the point where it
      // was seen (its position was relative to the container)
      copy.position = { x: parent.position.x + n.position.x, y: parent.position.y + n.position.y }
      delete copy.parentNode
    }
    out.push(copy)
  }
  return {
    v: 1,
    nodes: out,
    edges: edges.filter((e) => chosen.has(e.source) && chosen.has(e.target)).map(clone),
  }
}

/**
 * A copy ready to add to the canvas: new ids, position moved, edges stitched again.
 * `allow` drops the nodes whoever pastes cannot add.
 */
export function instantiateClip(
  clip: FlowClip,
  newId: (node: ClipNode) => string,
  offset: { x: number; y: number },
  allow: (node: ClipNode) => boolean = () => true,
): { nodes: ClipNode[]; edges: ClipEdge[] } {
  const original = new Map(clip.nodes.map((n) => [n.id, n]))
  const kept = clip.nodes.filter(allow)
  const ids = new Map(kept.map((n) => [n.id, newId(n)]))

  const nodes = kept.map((n) => {
    const copy = clone(n)
    copy.id = ids.get(n.id)!
    const parent = n.parentNode ? original.get(n.parentNode) : undefined
    if (n.parentNode && ids.has(n.parentNode)) {
      copy.parentNode = ids.get(n.parentNode) // inside the copied container: relative position, unchanged
    } else {
      // free node, or a child left without its container (dropped): absolute position
      delete copy.parentNode
      const base = parent ? { x: parent.position.x + n.position.x, y: parent.position.y + n.position.y } : n.position
      copy.position = { x: base.x + offset.x, y: base.y + offset.y }
    }
    return copy
  })
  // the containers before the children: the canvas wants them in this order
  nodes.sort((a, b) => Number(!!a.parentNode) - Number(!!b.parentNode))

  const edges = clip.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => {
      const source = ids.get(e.source)!
      const target = ids.get(e.target)!
      return { ...clone(e), id: `e-${source}-${e.targetHandle || 'left'}-${target}`, source, target }
    })
  return { nodes, edges }
}

export function writeClip(clip: FlowClip): void {
  memory = clip
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(clip))
  } catch {
    // the copy in memory stays: it holds for this tab
  }
}

export function readClip(): FlowClip | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const clip = JSON.parse(raw)
      if (clip?.v === 1 && Array.isArray(clip.nodes) && Array.isArray(clip.edges) && clip.nodes.length) return clip
    }
  } catch {
    // unreadable content or localStorage missing: it falls back on the memory
  }
  return memory
}
