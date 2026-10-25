// The canvas's auto-layout ("Tidy the flow"). No external library: the graph is an
// almost-linear DAG, so a walk is enough.
//
// Scheme: the main chain runs left→right on a row; every right branch (the right side of
// a join, a foreach's driver) goes on a row ABOVE, with its last node aligned in X with
// the node it feeds (the edge comes down into the top input). The children of a foreach
// container are lined up inside the container, which resizes to hold them.

import type { Edge, GraphNode } from '@vue-flow/core'
import { buildIncoming } from './useFlowModel'

const GAP_X = 70 // horizontal space between consecutive nodes
const GAP_Y = 70 // vertical space between rows
const BASE = 60 // margin from the canvas's origin
const OP_W = 190 // fallback when the node has not been measured yet
const OP_H = 80
const PAD = 24 // inner padding of the foreach container
const HEADER = 56 // room for the container's title bar

interface Size {
  width: number
  height: number
}

export interface LayoutResult {
  positions: Map<string, { x: number; y: number }> // top-level nodes (absolute)
  childPositions: Map<string, { x: number; y: number }> // foreach children (relative to the container)
  containerSizes: Map<string, Size>
}

// GraphNode and not Node: the sizes measured from the DOM sit on the LIVE node
function widthOf(n: GraphNode, containers: Map<string, Size>): number {
  const c = containers.get(n.id)
  if (c) return c.width
  if (n.dimensions?.width) return n.dimensions.width
  if (n.type === 'foreach') return parseFloat(String((n.style as any)?.width)) || 460
  return OP_W
}

function heightOf(n: GraphNode, containers: Map<string, Size>): number {
  const c = containers.get(n.id)
  if (c) return c.height
  if (n.dimensions?.height) return n.dimensions.height
  if (n.type === 'foreach') return parseFloat(String((n.style as any)?.height)) || 280
  return OP_H
}

/** A container's children in chain order (then any loose ones). */
function orderedChildren(nodes: GraphNode[], edges: Edge[], containerId: string): GraphNode[] {
  // comments are annotations: they do not enter the body's chain
  const kids = nodes.filter((n) => n.parentNode === containerId && n.type !== 'comment')
  if (!kids.length) return []
  const ids = new Set(kids.map((k) => k.id))
  const next = new Map<string, string>()
  const hasIn = new Set<string>()
  for (const e of edges) {
    if (ids.has(e.source) && ids.has(e.target) && (((e.targetHandle as string) || 'left') === 'left')) {
      next.set(e.source, e.target)
      hasIn.add(e.target)
    }
  }
  const out: GraphNode[] = []
  const seen = new Set<string>()
  let cur = kids.find((k) => !hasIn.has(k.id))?.id
  while (cur && !seen.has(cur)) {
    seen.add(cur)
    const n = kids.find((k) => k.id === cur)
    if (n) out.push(n)
    cur = next.get(cur)
  }
  for (const k of kids) if (!seen.has(k.id)) out.push(k)
  return out
}

export function computeAutoLayout(nodes: GraphNode[], edges: Edge[]): LayoutResult {
  const inc = buildIncoming(edges)
  // the comments stay where the user put them: outside the auto-layout
  const top = nodes.filter((n) => !n.parentNode && n.type !== 'comment')
  const byId = new Map(top.map((n) => [n.id, n]))

  // 1) foreach containers: children lined up, sizes fitted to the content
  const childPositions = new Map<string, { x: number; y: number }>()
  const containerSizes = new Map<string, Size>()
  for (const c of top.filter((n) => n.type === 'foreach')) {
    const kids = orderedChildren(nodes, edges, c.id)
    if (!kids.length) continue // empty: leaves the sizes it has (room for the drop)
    let x = PAD
    let maxH = 0
    for (const k of kids) {
      childPositions.set(k.id, { x, y: HEADER })
      x += (k.dimensions?.width || OP_W) + 48
      maxH = Math.max(maxH, k.dimensions?.height || OP_H)
    }
    containerSizes.set(c.id, {
      width: Math.max(460, x - 48 + PAD),
      height: Math.max(220, HEADER + maxH + PAD),
    })
  }

  // 2) components: from every leaf it walks up the left chain; the right branches
  //    recursively on rows above (negative local rows, then normalized)
  const hasOut = new Set(edges.map((e) => e.source))
  let leaves = top.filter((n) => !hasOut.has(n.id))
  if (!leaves.length) leaves = top // paranoia: a graph without leaves

  const globalX = new Map<string, number>()
  const globalRow = new Map<string, number>()
  let nextRow = 0

  for (const leaf of leaves) {
    if (globalRow.has(leaf.id)) continue
    const localX = new Map<string, number>()
    const localRow = new Map<string, number>()
    let cursor = 0 // every right branch takes the first free row above

    const placeChain = (leafId: string, row: number, alignRightX: number | null) => {
      const chain: GraphNode[] = []
      const walked = new Set<string>()
      let cur: string | undefined = leafId
      while (cur && !walked.has(cur) && !localRow.has(cur) && !globalRow.has(cur)) {
        walked.add(cur)
        const n = byId.get(cur)
        if (!n) break
        chain.unshift(n)
        cur = inc.get(cur)?.left
      }
      if (!chain.length) return
      // fan-out: the chain hooks onto a node already placed in this component
      const stopAt = cur && localRow.has(cur) ? cur : null

      const xs: number[] = new Array(chain.length)
      if (alignRightX != null) {
        // branch: the last node aligned with the fed node, the earlier ones on the left
        let x = alignRightX
        for (let i = chain.length - 1; i >= 0; i--) {
          xs[i] = x
          if (i > 0) x -= GAP_X + widthOf(chain[i - 1], containerSizes)
        }
      } else {
        let x = stopAt ? localX.get(stopAt)! + widthOf(byId.get(stopAt)!, containerSizes) + GAP_X : 0
        for (let i = 0; i < chain.length; i++) {
          xs[i] = x
          x += widthOf(chain[i], containerSizes) + GAP_X
        }
      }
      chain.forEach((n, i) => {
        localX.set(n.id, xs[i])
        localRow.set(n.id, row)
      })
      for (let i = 0; i < chain.length; i++) {
        const rightId = inc.get(chain[i].id)?.right
        if (rightId && !localRow.has(rightId) && !globalRow.has(rightId)) {
          placeChain(rightId, --cursor, xs[i])
        }
      }
    }

    placeChain(leaf.id, 0, null)

    // normalizes the component (minimum x at 0, rows [0..span-1]) and pours it into the
    // global one in a band under the components already placed
    let minRow = 0
    let minX = Infinity
    for (const r of localRow.values()) minRow = Math.min(minRow, r)
    for (const x of localX.values()) minX = Math.min(minX, x)
    for (const [id, r] of localRow) {
      globalRow.set(id, nextRow + (r - minRow))
      globalX.set(id, localX.get(id)! - minX)
    }
    nextRow += -minRow + 1
  }

  // 3) rows → absolute y, every row as tall as its tallest node
  const rowHeight = new Map<number, number>()
  for (const n of top) {
    const r = globalRow.get(n.id)
    if (r == null) continue
    rowHeight.set(r, Math.max(rowHeight.get(r) ?? 0, heightOf(n, containerSizes)))
  }
  const rowY = new Map<number, number>()
  let y = BASE
  for (let r = 0; r < nextRow; r++) {
    rowY.set(r, y)
    y += (rowHeight.get(r) ?? OP_H) + GAP_Y
  }

  const positions = new Map<string, { x: number; y: number }>()
  for (const n of top) {
    const r = globalRow.get(n.id)
    if (r == null) continue
    positions.set(n.id, { x: BASE + globalX.get(n.id)!, y: rowY.get(r)! })
  }
  return { positions, childPositions, containerSizes }
}
