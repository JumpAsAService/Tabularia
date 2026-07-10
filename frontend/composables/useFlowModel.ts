// Modello del flow: traversata del grafo, serializzazione in IR, spec dei form.
//
// Il grafo è un DAG "quasi lineare": la catena principale segue l'handle di
// input sinistro ('left'); un nodo `join` ha un secondo input destro ('right')
// che punta a una SORGENTE (dataset grezzo) da unire. Join contro una catena
// trasformata richiederebbe supporto backend (sub-flow) → per ora: sorgente.

import type { Node, Edge } from '@vue-flow/core'
import type { Operation } from './useApi'

export const SOURCE_ID = 'source'

export interface Incoming {
  left?: string
  right?: string
}

/** Mappa target -> {left, right} in base al targetHandle degli archi. */
export function buildIncoming(edges: Edge[]): Map<string, Incoming> {
  const m = new Map<string, Incoming>()
  for (const e of edges) {
    const handle = (e.targetHandle as string) || 'left'
    const entry = m.get(e.target) ?? {}
    entry[handle === 'right' ? 'right' : 'left'] = e.source
    m.set(e.target, entry)
  }
  return m
}

/**
 * Risolve la catena che termina in `targetId`: risale gli input sinistri fino
 * alla sorgente, raccogliendo i nodi-operazione. Ritorna la sorgente terminale
 * e la lista IR delle operazioni (con il `right` dei join già iniettato).
 */
export function resolveChain(
  nodes: Node[],
  edges: Edge[],
  targetId: string,
): { sourceNode: Node | null; operations: Operation[] } {
  const inc = buildIncoming(edges)

  // Nodo DENTRO un container foreach: la catena è quella a monte del container
  // + il foreach col corpo troncato a questo nodo (la preview mostra il ciclo
  // eseguito fino a qui, su tutte le iterazioni).
  const target = nodes.find((n) => n.id === targetId)
  if (target?.parentNode) {
    const upstreamId = inc.get(target.parentNode)?.left
    const upstream = upstreamId
      ? resolveChain(nodes, edges, upstreamId)
      : { sourceNode: null as Node | null, operations: [] as Operation[] }
    const op = operationFor(nodes, edges, target.parentNode, targetId)
    return { sourceNode: upstream.sourceNode, operations: [...upstream.operations, op] }
  }

  const opIds: string[] = []
  const seen = new Set<string>()
  let sourceNode: Node | null = null
  let cur: string | undefined = targetId

  while (cur && !seen.has(cur)) {
    seen.add(cur)
    const node = nodes.find((n) => n.id === cur)
    if (!node) break
    if (node.type === 'source') {
      sourceNode = node
      break
    }
    if (node.type === 'operation' || node.type === 'foreach') opIds.push(cur)
    cur = inc.get(cur)?.left
  }

  opIds.reverse()
  const operations = opIds.map((id) => operationFor(nodes, edges, id))
  return { sourceNode, operations }
}

/**
 * Costruisce l'operazione IR di un nodo. Per i join, il lato destro diventa un
 * *sotto-flow* (sorgente + operazioni del ramo destro), risolto ricorsivamente:
 * così si può unire anche un ramo trasformato, incluso uno che parte dalla
 * stessa sorgente del ramo sinistro.
 */
function operationFor(nodes: Node[], edges: Edge[], id: string, bodyUntil?: string): Operation {
  const node = nodes.find((n) => n.id === id)!
  const params: Record<string, any> = { ...(node.data.params ?? {}) }

  if (node.data.opType === 'join' || node.data.opType === 'union') {
    const rightId = buildIncoming(edges).get(id)?.right
    if (rightId) {
      const right = resolveChain(nodes, edges, rightId)
      if (right.sourceNode?.data?.parquetKey) {
        params.right = {
          source: { bucket: right.sourceNode.data.bucket, key: right.sourceNode.data.parquetKey },
          operations: right.operations,
        }
      }
    }
  }

  if (node.type === 'foreach') {
    // driver = ramo collegato all'input in alto (handle 'right'): ogni riga
    // un'iterazione, le colonne sono i placeholder disponibili
    const driverId = buildIncoming(edges).get(id)?.right
    if (driverId) {
      const drv = resolveChain(nodes, edges, driverId)
      if (drv.sourceNode?.data?.parquetKey) {
        params.driver = {
          source: { bucket: drv.sourceNode.data.bucket, key: drv.sourceNode.data.parquetKey },
          operations: drv.operations,
        }
      }
    }
    // corpo = catena dei nodi figli dentro il container
    params.body = containerBody(nodes, edges, id, bodyUntil)
  }
  return { type: node.data.opType, params }
}

/**
 * Catena LINEARE dei nodi figli di un container foreach: parte dal figlio senza
 * input sinistro interno e segue gli archi tra figli. `until` tronca il corpo a
 * quel nodo incluso (per la preview di un nodo interno).
 */
function containerBody(nodes: Node[], edges: Edge[], containerId: string, until?: string): Operation[] {
  const children = nodes.filter((n) => n.parentNode === containerId)
  if (!children.length) return []
  const childIds = new Set(children.map((c) => c.id))
  const inc = buildIncoming(edges)

  // archi interni (solo left→left tra figli)
  const next = new Map<string, string>()
  for (const e of edges) {
    if (childIds.has(e.source) && childIds.has(e.target) && (((e.targetHandle as string) || 'left') === 'left')) {
      next.set(e.source, e.target)
    }
  }

  let cur = children.find((c) => !childIds.has(inc.get(c.id)?.left ?? ''))?.id
  const ops: Operation[] = []
  const seen = new Set<string>()
  while (cur && !seen.has(cur)) {
    seen.add(cur)
    ops.push(operationFor(nodes, edges, cur))
    if (cur === until) break
    cur = next.get(cur)
  }
  return ops
}

/** Il nodo foglia dell'output principale (senza archi uscenti). I figli dei
 * container non contano: appartengono al corpo del ciclo, non alla catena. */
export function leafNodeId(nodes: Node[], edges: Edge[]): string {
  const hasOutgoing = new Set(edges.map((e) => e.source))
  const leaves = nodes.filter((n) => !hasOutgoing.has(n.id) && !n.parentNode)
  return (
    leaves.find((n) => n.type === 'operation' || n.type === 'foreach') ?? leaves[0]
  )?.id ?? SOURCE_ID
}

// ── Spec dei form per operazione ──────────────────────────────────────────
// control: come renderizzare il campo. Le colonne provengono dallo schema a
// monte (input del nodo), non da testo libero.
export interface FieldSpec {
  key: string
  label: string
  control:
    | 'column' // select singola colonna
    | 'columns' // multi-select colonne
    | 'operator'
    | 'func'
    | 'dtype'
    | 'how'
    | 'text'
    | 'number'
    | 'boolean'
    | 'value' // valore filtro (parsing intelligente)
    | 'castlist' // {colonna: dtype}
    | 'renamelist' // {da: a}
    | 'filllist' // {colonna: valore}
    | 'agglist' // [{column, func, alias}]
    | 'exprlist' // [{name, expr}] — colonne calcolate (espressioni SQL)
    | 'strategy' // union: relaxed | strict
    | 'json' // textarea con JSON libero (es. items del foreach)
  optional?: boolean
}

export const OP_SPECS: Record<string, FieldSpec[]> = {
  select: [{ key: 'columns', label: 'Colonne da tenere', control: 'columns' }],
  drop: [{ key: 'columns', label: 'Colonne da rimuovere', control: 'columns' }],
  rename: [{ key: 'mapping', label: 'Rinomina', control: 'renamelist' }],
  cast: [{ key: 'columns', label: 'Conversioni di tipo', control: 'castlist' }],
  filter: [
    { key: 'column', label: 'Colonna', control: 'column' },
    { key: 'operator', label: 'Operatore', control: 'operator' },
    { key: 'value', label: 'Valore', control: 'value' },
  ],
  sort: [
    { key: 'by', label: 'Ordina per', control: 'column' },
    { key: 'descending', label: 'Discendente', control: 'boolean' },
  ],
  limit: [{ key: 'n', label: 'Numero di righe', control: 'number' }],
  unique: [{ key: 'subset', label: 'Colonne (vuoto = tutte)', control: 'columns', optional: true }],
  fill_null: [{ key: 'columns', label: 'Riempi i null', control: 'filllist' }],
  drop_nulls: [{ key: 'subset', label: 'Colonne (vuoto = tutte)', control: 'columns', optional: true }],
  group_by: [
    { key: 'by', label: 'Raggruppa per', control: 'columns' },
    { key: 'aggregations', label: 'Aggregazioni', control: 'agglist' },
  ],
  pivot: [
    { key: 'index', label: 'Chiavi di riga', control: 'columns' },
    { key: 'on', label: 'Colonne dai valori di', control: 'column' },
    { key: 'values', label: 'Valori da', control: 'column' },
    { key: 'func', label: 'Aggrega con', control: 'func' },
  ],
  unpivot: [
    { key: 'index', label: 'Colonne da tenere fisse', control: 'columns', optional: true },
    { key: 'on', label: 'Colonne da sciogliere (vuoto = tutte le altre)', control: 'columns', optional: true },
    { key: 'variable_name', label: 'Nome colonna etichette', control: 'text', optional: true },
    { key: 'value_name', label: 'Nome colonna valori', control: 'text', optional: true },
  ],
  compute: [
    { key: 'columns', label: 'Colonne calcolate (espressioni SQL)', control: 'exprlist' },
  ],
  join: [
    { key: 'how', label: 'Tipo di join', control: 'how' },
    { key: 'on', label: 'Colonne chiave (in entrambe)', control: 'columns' },
  ],
  union: [
    { key: 'strategy', label: 'Se le colonne dei due rami differiscono', control: 'strategy' },
  ],
  foreach: [
    { key: 'items', label: 'Iterazioni statiche (solo senza driver)', control: 'json', optional: true },
    { key: 'add_keys_as_columns', label: 'Aggiungi i placeholder come colonne', control: 'boolean' },
  ],
}

export const FILTER_OPERATORS = [
  'eq', 'ne', 'gt', 'ge', 'lt', 'le', 'in', 'not_in',
  'between', 'contains', 'starts_with', 'ends_with', 'is_null', 'is_not_null',
]
export const AGG_FUNCS = [
  'sum', 'mean', 'min', 'max', 'count', 'median', 'std', 'var', 'first', 'last', 'n_unique',
]
export const DTYPES = ['int', 'float', 'str', 'bool', 'date', 'datetime']
export const JOIN_HOWS = ['inner', 'left', 'right', 'full', 'semi', 'anti', 'cross']

// ── Human-readable dropdown labels (the VALUE stays the backend id) ─────────
export const OPERATOR_LABELS: Record<string, string> = {
  eq: 'equals',
  ne: 'not equal to',
  gt: 'greater than',
  ge: 'greater or equal to',
  lt: 'less than',
  le: 'less or equal to',
  in: 'is one of…',
  not_in: 'is not one of…',
  between: 'between',
  contains: 'contains',
  starts_with: 'starts with',
  ends_with: 'ends with',
  is_null: 'is null',
  is_not_null: 'is not null',
}
export const AGG_LABELS: Record<string, string> = {
  sum: 'sum',
  mean: 'average',
  min: 'minimum',
  max: 'maximum',
  count: 'count',
  median: 'median',
  std: 'std deviation',
  var: 'variance',
  first: 'first',
  last: 'last',
  n_unique: 'distinct count',
}
export const HOW_LABELS: Record<string, string> = {
  inner: 'inner — matching rows only',
  left: 'left — all rows from the left',
  right: 'right — all rows from the right',
  full: 'full — all rows from both sides',
  semi: 'semi — left rows with a match (no right columns)',
  anti: 'anti — left rows WITHOUT a match',
  cross: 'cross — cartesian product',
}
export const STRATEGY_LABELS: Record<string, string> = {
  relaxed: 'align by name — missing columns become null',
  strict: 'require identical schemas',
}
export const DTYPE_LABELS: Record<string, string> = {
  int: 'integer',
  float: 'decimal',
  str: 'text',
  bool: 'boolean',
  date: 'date',
  datetime: 'datetime',
}

/** Operatori di filtro che non richiedono un valore. */
export const NO_VALUE_OPERATORS = new Set(['is_null', 'is_not_null'])

/** Parametri di default sensati quando si sceglie un'operazione. */
export function defaultParams(opType: string): Record<string, any> {
  switch (opType) {
    case 'filter':
      return { operator: 'eq' }
    case 'join':
      return { how: 'inner' }
    case 'union':
      return { strategy: 'relaxed' }
    case 'sort':
      return { descending: false }
    case 'foreach':
      return { add_keys_as_columns: true }
    default:
      return {}
  }
}
