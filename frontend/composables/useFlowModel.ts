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

/** Mappa target -> {left, right} in base al targetHandle degli archi DATI. Gli
 * archi di SEQUENZA (verticali, handle seq-in/seq-out) definiscono l'ordine di
 * orchestrazione e NON fanno parte della pipeline dati: qui vanno ignorati. */
export function buildIncoming(edges: Edge[]): Map<string, Incoming> {
  const m = new Map<string, Incoming>()
  for (const e of edges) {
    const handle = (e.targetHandle as string) || 'left'
    if (handle === 'seq-in' || (e.sourceHandle as string) === 'seq-out') continue
    const entry = m.get(e.target) ?? {}
    entry[handle === 'right' ? 'right' : 'left'] = e.source
    m.set(e.target, entry)
  }
  return m
}

// ── Filtri A MONTE di un nodo sorgente ────────────────────────────────────
// `data.filters` = [{ id, column, operator, value }, …]: condizioni in AND
// applicate subito dopo la lettura della sorgente, PRIMA di ogni altra
// operazione (campione compreso). A differenza del campione fanno parte del
// flusso: valgono in sviluppo E in produzione (e nell'export dbt). Diventano
// normali operazioni `filter` senza marcatore. Speculare a
// `source_filter_operations` in gateway/services/flow_resolver.py.
export type SourceFilter = { id: string; column?: string; operator?: string; value?: any }

/** Una condizione è completa se ha colonna, operatore e (dove serve) valore. */
export function isCompleteSourceFilter(f: SourceFilter | null | undefined): boolean {
  if (!f || typeof f !== 'object') return false
  if (!f.column || !f.operator || !FILTER_OPERATORS.includes(f.operator)) return false
  if (NO_VALUE_OPERATORS.has(f.operator)) return true
  if (f.value === null || f.value === undefined) return false
  if ((f.operator === 'in' || f.operator === 'not_in') && (!Array.isArray(f.value) || !f.value.length)) return false
  if (f.operator === 'between' && (!Array.isArray(f.value) || f.value.length !== 2)) return false
  return true
}

export function sourceFilterOperations(data: any): Operation[] {
  const filters: SourceFilter[] = Array.isArray(data?.filters) ? data.filters : []
  return filters.filter(isCompleteSourceFilter).map((f) => {
    const params: Record<string, any> = { column: f.column, operator: f.operator }
    if (!NO_VALUE_OPERATORS.has(f.operator!)) params.value = f.value
    return { type: 'filter', params }
  })
}

/** Numero di condizioni a monte COMPLETE (badge sul nodo). */
export function sourceFilterCount(data: any): number {
  return sourceFilterOperations(data).length
}

// ── Campione di SVILUPPO di un nodo sorgente ──────────────────────────────
// `data.sample` = { mode: 'first' | 'random', rows?, percent? } | null. L'editor
// lavora SEMPRE in sviluppo: il campione diventa la prima operazione dopo la
// sorgente (limit / sample) con il marcatore `_dev_sample`. In produzione
// (scheduler, "esegui in produzione") il gateway NON lo inietta e rimuove
// comunque ogni operazione marcata: i flussi di produzione girano su tutti i
// record. Speculare a `dev_sample_operation` in gateway/services/flow_resolver.py.
export type SourceSample = { mode: 'first' | 'random'; rows?: number | null; percent?: number | null }
export const DEV_SAMPLE_MARK = '_dev_sample'

export function devSampleOperation(data: any): Operation | null {
  const s: SourceSample | null | undefined = data?.sample
  if (!s || typeof s !== 'object') return null
  if (s.mode === 'first') {
    const n = Math.floor(Number(s.rows ?? 0))
    if (!(n > 0)) return null
    return { type: 'limit', params: { n, [DEV_SAMPLE_MARK]: true } }
  }
  if (s.mode === 'random') {
    const pct = Number(s.percent ?? 0)
    if (!(pct > 0 && pct < 100)) return null
    return { type: 'sample', params: { fraction: pct / 100, seed: 42, [DEV_SAMPLE_MARK]: true } }
  }
  return null
}

/** Etichetta breve del campione attivo (badge sul nodo), o null. */
export function sampleLabel(data: any, t: (k: string, p?: any) => string): string | null {
  const s: SourceSample | null | undefined = data?.sample
  if (!s) return null
  if (s.mode === 'first' && Number(s.rows) > 0) return t('sourceNode.sampleFirst', { n: Number(s.rows).toLocaleString() })
  if (s.mode === 'random' && Number(s.percent) > 0 && Number(s.percent) < 100) return t('sourceNode.sampleRandom', { p: s.percent })
  return null
}

/**
 * Risolve la catena che termina in `targetId`: risale gli input sinistri fino
 * alla sorgente, raccogliendo i nodi-operazione. Ritorna la sorgente terminale
 * e la lista IR delle operazioni (con il `right` dei join già iniettato; in
 * testa i filtri a monte della sorgente e poi il campione di sviluppo, se
 * impostati).
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
  // testa della catena: filtri a monte (sempre), poi il campione (solo editor)
  const sample = sourceNode ? devSampleOperation(sourceNode.data) : null
  if (sample) operations.unshift(sample)
  if (sourceNode) operations.unshift(...sourceFilterOperations(sourceNode.data))
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
  // i commenti dentro un container sono solo annotazioni: fuori dal corpo
  const children = nodes.filter((n) => n.parentNode === containerId && n.type !== 'comment')
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
  // i commenti sono annotazioni: non sono nodi del flusso, mai una foglia
  const leaves = nodes.filter((n) => !hasOutgoing.has(n.id) && !n.parentNode && n.type !== 'comment')
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
    | 'joinkeys' // join: righe colonna sinistra → colonna destra (nomi anche diversi)
    | 'reorder' // riordino colonne con drag-and-drop
    | 'json' // textarea con JSON libero (es. items del foreach)
    | 'sqltext' // textarea con una query SQL intera (Execute SQL); preview MANUALE
  optional?: boolean
}

export const OP_SPECS: Record<string, FieldSpec[]> = {
  select: [{ key: 'columns', label: 'params.select_columns', control: 'columns' }],
  reorder: [{ key: 'columns', label: 'params.reorder_columns', control: 'reorder' }],
  drop: [{ key: 'columns', label: 'params.drop_columns', control: 'columns' }],
  rename: [{ key: 'mapping', label: 'params.rename_mapping', control: 'renamelist' }],
  cast: [{ key: 'columns', label: 'params.cast_columns', control: 'castlist' }],
  filter: [
    { key: 'column', label: 'params.filter_column', control: 'column' },
    { key: 'operator', label: 'params.filter_operator', control: 'operator' },
    { key: 'value', label: 'params.filter_value', control: 'value' },
  ],
  sort: [
    { key: 'by', label: 'params.sort_by', control: 'column' },
    { key: 'descending', label: 'params.sort_descending', control: 'boolean' },
  ],
  limit: [{ key: 'n', label: 'params.limit_n', control: 'number' }],
  unique: [{ key: 'subset', label: 'params.unique_subset', control: 'columns', optional: true }],
  fill_null: [{ key: 'columns', label: 'params.fill_null_columns', control: 'filllist' }],
  drop_nulls: [{ key: 'subset', label: 'params.drop_nulls_subset', control: 'columns', optional: true }],
  group_by: [
    { key: 'by', label: 'params.group_by_by', control: 'columns' },
    { key: 'aggregations', label: 'params.group_by_aggregations', control: 'agglist' },
  ],
  pivot: [
    { key: 'index', label: 'params.pivot_index', control: 'columns' },
    // più colonne = combinazioni (etichette unite da '_', vedi standard cross-engine)
    { key: 'on', label: 'params.pivot_on', control: 'columns' },
    { key: 'values', label: 'params.pivot_values', control: 'column' },
    { key: 'func', label: 'params.pivot_func', control: 'func' },
  ],
  unpivot: [
    { key: 'index', label: 'params.unpivot_index', control: 'columns', optional: true },
    { key: 'on', label: 'params.unpivot_on', control: 'columns', optional: true },
    { key: 'variable_name', label: 'params.unpivot_variable_name', control: 'text', optional: true },
    { key: 'value_name', label: 'params.unpivot_value_name', control: 'text', optional: true },
  ],
  compute: [
    { key: 'columns', label: 'params.compute_columns', control: 'exprlist' },
  ],
  sql: [{ key: 'query', label: 'params.sql_query', control: 'sqltext' }],
  join: [
    { key: 'how', label: 'params.join_how', control: 'how' },
    { key: 'keys', label: 'params.join_keys', control: 'joinkeys' },
  ],
  union: [
    { key: 'strategy', label: 'params.union_strategy', control: 'strategy' },
  ],
  foreach: [
    { key: 'items', label: 'params.foreach_items', control: 'json', optional: true },
    { key: 'add_keys_as_columns', label: 'params.foreach_add_keys_as_columns', control: 'boolean' },
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
    case 'sql':
      return { query: '' }
    default:
      return {}
  }
}
