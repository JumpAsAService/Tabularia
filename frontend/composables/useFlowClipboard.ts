// Copia e incolla dei nodi dell'editor.
//
// Ciò che si copia è un pezzo di flusso nella stessa forma in cui si salva: i
// nodi scelti, i figli dei container scelti, e gli archi che stanno TUTTI dentro
// la selezione (un arco verso un nodo non copiato non avrebbe dove attaccarsi).
// Sta nel localStorage e non in una variabile: così si incolla anche in un altro
// flusso o in un'altra scheda. Non si usa la clipboard di sistema: chiede un
// permesso al browser, e a chi incolla in un documento arriverebbe del JSON.

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
let memoria: FlowClip | null = null // se il localStorage non c'è (navigazione privata, quota)

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v))

/** Il pezzo di flusso da copiare. `null` se non c'è niente di selezionato. */
export function buildClip(nodes: ClipNode[], edges: ClipEdge[], selected: string[]): FlowClip | null {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const chosen = new Set(selected.filter((id) => byId.has(id)))
  // un container si porta dietro il suo contenuto
  for (const n of nodes) if (n.parentNode && chosen.has(n.parentNode)) chosen.add(n.id)
  if (!chosen.size) return null

  const out: ClipNode[] = []
  for (const n of nodes) {
    if (!chosen.has(n.id)) continue
    const copy = clone(n)
    const parent = n.parentNode ? byId.get(n.parentNode) : undefined
    if (parent && !chosen.has(parent.id)) {
      // figlio copiato senza il suo container: diventa un nodo libero, nel punto
      // in cui si vedeva (la sua posizione era relativa al container)
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

/** Una copia pronta da aggiungere al canvas: id nuovi, posizione spostata, archi ricuciti.
 *  `allow` scarta i nodi che chi incolla non può aggiungere. */
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
      copy.parentNode = ids.get(n.parentNode) // dentro il container copiato: posizione relativa, invariata
    } else {
      // nodo libero, o figlio rimasto senza container (scartato): posizione assoluta
      delete copy.parentNode
      const base = parent ? { x: parent.position.x + n.position.x, y: parent.position.y + n.position.y } : n.position
      copy.position = { x: base.x + offset.x, y: base.y + offset.y }
    }
    return copy
  })
  // i container prima dei figli: il canvas li vuole in quest'ordine
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
  memoria = clip
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(clip))
  } catch {
    // resta la copia in memoria: vale per questa scheda
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
    // contenuto illeggibile o localStorage assente: si ripiega sulla memoria
  }
  return memoria
}
