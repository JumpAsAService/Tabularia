// Auto-layout del canvas ("Ordina il flusso"). Nessuna libreria esterna:
// il grafo è un DAG quasi-lineare, quindi basta una passeggiata.
//
// Schema: la catena principale scorre sinistra→destra su una riga; ogni ramo
// destro (lato right di un join, driver di un foreach) va su una riga SOPRA,
// col suo ultimo nodo allineato in X al nodo che alimenta (l'arco scende
// nell'input in alto). I figli di un container foreach vengono messi in fila
// dentro il container, che si ridimensiona per contenerli.

import type { Node, Edge } from '@vue-flow/core'
import { buildIncoming } from './useFlowModel'

const GAP_X = 70 // spazio orizzontale tra nodi consecutivi
const GAP_Y = 70 // spazio verticale tra righe
const BASE = 60 // margine dall'origine del canvas
const OP_W = 190 // fallback se il nodo non è ancora stato misurato
const OP_H = 80
const PAD = 24 // padding interno del container foreach
const HEADER = 56 // spazio per la barra del titolo del container

interface Size {
  width: number
  height: number
}

export interface LayoutResult {
  positions: Map<string, { x: number; y: number }> // nodi top-level (assolute)
  childPositions: Map<string, { x: number; y: number }> // figli foreach (relative al container)
  containerSizes: Map<string, Size>
}

function widthOf(n: Node, containers: Map<string, Size>): number {
  const c = containers.get(n.id)
  if (c) return c.width
  if (n.dimensions?.width) return n.dimensions.width
  if (n.type === 'foreach') return parseFloat(String((n.style as any)?.width)) || 460
  return OP_W
}

function heightOf(n: Node, containers: Map<string, Size>): number {
  const c = containers.get(n.id)
  if (c) return c.height
  if (n.dimensions?.height) return n.dimensions.height
  if (n.type === 'foreach') return parseFloat(String((n.style as any)?.height)) || 280
  return OP_H
}

/** Figli di un container in ordine di catena (poi gli eventuali sciolti). */
function orderedChildren(nodes: Node[], edges: Edge[], containerId: string): Node[] {
  const kids = nodes.filter((n) => n.parentNode === containerId)
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
  const out: Node[] = []
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

export function computeAutoLayout(nodes: Node[], edges: Edge[]): LayoutResult {
  const inc = buildIncoming(edges)
  const top = nodes.filter((n) => !n.parentNode)
  const byId = new Map(top.map((n) => [n.id, n]))

  // 1) container foreach: figli in fila, dimensioni adattate al contenuto
  const childPositions = new Map<string, { x: number; y: number }>()
  const containerSizes = new Map<string, Size>()
  for (const c of top.filter((n) => n.type === 'foreach')) {
    const kids = orderedChildren(nodes, edges, c.id)
    if (!kids.length) continue // vuoto: lascia le dimensioni che ha (spazio per il drop)
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

  // 2) componenti: da ogni foglia si risale la catena sinistra; i rami destri
  //    ricorsivamente su righe sopra (righe locali negative, poi normalizzate)
  const hasOut = new Set(edges.map((e) => e.source))
  let leaves = top.filter((n) => !hasOut.has(n.id))
  if (!leaves.length) leaves = top // paranoia: grafo senza foglie

  const globalX = new Map<string, number>()
  const globalRow = new Map<string, number>()
  let nextRow = 0

  for (const leaf of leaves) {
    if (globalRow.has(leaf.id)) continue
    const localX = new Map<string, number>()
    const localRow = new Map<string, number>()
    let cursor = 0 // ogni ramo destro prende la prima riga libera sopra

    const placeChain = (leafId: string, row: number, alignRightX: number | null) => {
      const chain: Node[] = []
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
      // fan-out: la catena si aggancia a un nodo già piazzato in questo componente
      const stopAt = cur && localRow.has(cur) ? cur : null

      const xs: number[] = new Array(chain.length)
      if (alignRightX != null) {
        // ramo: l'ultimo nodo allineato al nodo alimentato, i precedenti a sinistra
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

    // normalizza il componente (x minima a 0, righe [0..span-1]) e riversa
    // nel globale in una banda sotto i componenti già piazzati
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

  // 3) righe → y assolute, ogni riga alta quanto il suo nodo più alto
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
