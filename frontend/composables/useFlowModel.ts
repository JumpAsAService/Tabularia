// The flow's model: walking the graph, serializing to IR, the forms' specs.
//
// The graph is an "almost linear" DAG: the main chain follows the left input handle
// ('left'); a `join` node has a second, right input ('right') pointing to a SOURCE (raw
// dataset) to join. A join against a transformed chain would need backend support
// (sub-flow) → for now: a source.

import type { Node, Edge } from '@vue-flow/core'
import type { Operation } from './useApi'

export const SOURCE_ID = 'source'

export interface Incoming {
  left?: string
  right?: string
}

/**
 * Map target -> {left, right} by the targetHandle of the DATA edges. The SEQUENCE edges
 * (vertical, handles seq-in/seq-out) define the orchestration's order and are NOT part of
 * the data pipeline: here they are ignored.
 */
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

// ── UPSTREAM filters of a source node ────────────────────────────────────
// `data.filters` = [{ id, column, operator, value }, …]: conditions in AND applied right
// after reading the source, BEFORE any other operation (sample included). Unlike the
// sample they are part of the flow: they hold in development AND in production (and in
// the dbt export). They become normal `filter` operations without a marker. The mirror
// of `source_filter_operations` in gateway/services/flow_resolver.py.
export type SourceFilter = { id: string; column?: string; operator?: string; value?: any }

/** A condition is complete when it has column, operator and (where needed) value. */
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

/** Number of COMPLETE upstream conditions (badge on the node). */
export function sourceFilterCount(data: any): number {
  return sourceFilterOperations(data).length
}

// ── DEVELOPMENT sample of a source node ──────────────────────────────
// `data.sample` = { mode: 'first' | 'random', rows?, percent? } | null. The editor ALWAYS
// works in development: the sample becomes the first operation after the source (limit /
// sample) with the `_dev_sample` marker. In production (scheduler, "run in production")
// the gateway does NOT inject it and removes every marked operation anyway: the
// production flows run on all the records. The mirror of `dev_sample_operation` in
// gateway/services/flow_resolver.py.
export type SourceSample = { mode: 'first' | 'random' | 'off'; rows?: number | null; percent?: number | null }
export const DEV_SAMPLE_MARK = '_dev_sample'

// AUTOMATIC sample (the gateway's PREVIEW__DEFAULT_SAMPLE_ROWS, through /system/info): a
// node WITHOUT `sample` reads only the first N rows as if it had asked; `{ mode: 'off' }`
// is the user's explicit turning off and means «all rows» even with the default on.
// 0 = off.
let _defaultSampleRows = 0
export function setDefaultSampleRows(n: number) {
  _defaultSampleRows = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
}
export function getDefaultSampleRows(): number {
  return _defaultSampleRows
}

export function devSampleOperation(data: any, defaultRows: number = _defaultSampleRows): Operation | null {
  const s: SourceSample | null | undefined = data?.sample
  if (!s || typeof s !== 'object') {
    return defaultRows > 0 ? { type: 'limit', params: { n: defaultRows, [DEV_SAMPLE_MARK]: true } } : null
  }
  if (s.mode === 'off') return null // turned off by the user: wins over the default
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

/** Short label of the active sample (badge on the node), or null. */
export function sampleLabel(data: any, t: (k: string, p?: any) => string): string | null {
  const s: SourceSample | null | undefined = data?.sample
  if (!s) {
    const n = _defaultSampleRows
    return n > 0 ? t('sourceNode.sampleAuto', { n: n.toLocaleString() }) : null
  }
  if (s.mode === 'first' && Number(s.rows) > 0) return t('sourceNode.sampleFirst', { n: Number(s.rows).toLocaleString() })
  if (s.mode === 'random' && Number(s.percent) > 0 && Number(s.percent) < 100) return t('sourceNode.sampleRandom', { p: s.percent })
  return null
}

/**
 * Resolves the chain ending in `targetId`: walks up the left inputs to the source,
 * collecting the operation nodes. Returns the terminal source and the IR list of the
 * operations (with the joins' `right` injected already; at the head the source's
 * upstream filters and then the development sample, when set).
 */
export function resolveChain(
  nodes: Node[],
  edges: Edge[],
  targetId: string,
  // a Script or Container node's latest output (a run of the flow, or the editor's «Run»): the
  // nodes after it read that, as if it were a source
  scriptSources: Record<string, { bucket: string; key: string }> = {},
): { sourceNode: Node | null; operations: Operation[]; afterScript?: string } {
  const inc = buildIncoming(edges)

  // A node INSIDE a foreach container: the chain is the one upstream of the container +
  // the foreach with the body cut at this node (the preview shows the loop run up to
  // here, over all the iterations).
  const target = nodes.find((n) => n.id === targetId)
  if (target?.parentNode) {
    const upstreamId = inc.get(target.parentNode)?.left
    const upstream = upstreamId
      ? resolveChain(nodes, edges, upstreamId, scriptSources)
      : { sourceNode: null as Node | null, operations: [] as Operation[] }
    const op = operationFor(nodes, edges, target.parentNode, targetId, scriptSources)
    return { sourceNode: upstream.sourceNode, operations: [...upstream.operations, op] }
  }

  const opIds: string[] = []
  const seen = new Set<string>()
  let sourceNode: Node | null = null
  let afterScript: string | undefined
  let cur: string | undefined = targetId

  while (cur && !seen.has(cur)) {
    seen.add(cur)
    const node = nodes.find((n) => n.id === cur)
    if (!node) break
    if (node.type === 'source') {
      sourceNode = node
      break
    }
    // a Script or Container node: what it wrote last time, when there is a last time
    if (node.type === 'script' || node.type === 'container') {
      const out = scriptSources[node.id]
      if (!out) return { sourceNode: null, operations: [], afterScript: node.id }
      sourceNode = { ...node, data: { bucket: out.bucket, parquetKey: out.key } } as Node
      afterScript = node.id
      break
    }
    if (node.type === 'operation' || node.type === 'foreach') opIds.push(cur)
    cur = inc.get(cur)?.left
  }

  opIds.reverse()
  const operations = opIds.map((id) => operationFor(nodes, edges, id, undefined, scriptSources))
  // head of the chain: upstream filters (always), then the sample (editor only)
  const sample = sourceNode ? devSampleOperation(sourceNode.data) : null
  if (sample) operations.unshift(sample)
  if (sourceNode) operations.unshift(...sourceFilterOperations(sourceNode.data))
  return { sourceNode, operations, afterScript }
}

/**
 * Builds a node's IR operation. For joins, the right side becomes a *sub-flow* (source +
 * the right branch's operations), resolved recursively: so a transformed branch can be
 * joined too, one starting from the same source as the left branch included.
 */
function operationFor(
  nodes: Node[], edges: Edge[], id: string, bodyUntil?: string,
  // a branch that starts after a Script or Container node (the right side of a join, a
  // foreach's driver) reads its last output too, as the main chain does
  scriptSources: Record<string, { bucket: string; key: string }> = {},
): Operation {
  const node = nodes.find((n) => n.id === id)!
  const params: Record<string, any> = { ...(node.data.params ?? {}) }

  if (node.data.opType === 'join' || node.data.opType === 'union') {
    const rightId = buildIncoming(edges).get(id)?.right
    if (rightId) {
      const right = resolveChain(nodes, edges, rightId, scriptSources)
      if (right.sourceNode?.data?.parquetKey) {
        params.right = {
          source: { bucket: right.sourceNode.data.bucket, key: right.sourceNode.data.parquetKey },
          operations: right.operations,
        }
      }
    }
  }

  if (node.type === 'foreach') {
    // driver = the branch connected to the top input (handle 'right'): every row an
    // iteration, the columns are the placeholders available
    const driverId = buildIncoming(edges).get(id)?.right
    if (driverId) {
      const drv = resolveChain(nodes, edges, driverId, scriptSources)
      if (drv.sourceNode?.data?.parquetKey) {
        params.driver = {
          source: { bucket: drv.sourceNode.data.bucket, key: drv.sourceNode.data.parquetKey },
          operations: drv.operations,
        }
      }
    }
    // body = the chain of the child nodes inside the container
    params.body = containerBody(nodes, edges, id, bodyUntil, scriptSources)
  }
  return { type: node.data.opType, params }
}

/**
 * LINEAR chain of a foreach container's child nodes: it starts from the child without an
 * internal left input and follows the edges between children. `until` cuts the body at
 * that node included (for an inner node's preview).
 */
function containerBody(nodes: Node[], edges: Edge[], containerId: string, until?: string,
  scriptSources: Record<string, { bucket: string; key: string }> = {}): Operation[] {
  // the comments inside a container are only annotations: outside the body
  const children = nodes.filter((n) => n.parentNode === containerId && n.type !== 'comment')
  if (!children.length) return []
  const childIds = new Set(children.map((c) => c.id))
  const inc = buildIncoming(edges)

  // internal edges (only left→left between children)
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
    ops.push(operationFor(nodes, edges, cur, undefined, scriptSources))
    if (cur === until) break
    cur = next.get(cur)
  }
  return ops
}

/**
 * The main output's leaf node (without outgoing edges). The containers' children do not
 * count: they belong to the loop's body, not to the chain.
 */
export function leafNodeId(nodes: Node[], edges: Edge[]): string {
  const hasOutgoing = new Set(edges.map((e) => e.source))
  // comments are annotations: they are not flow nodes, never a leaf
  const leaves = nodes.filter((n) => !hasOutgoing.has(n.id) && !n.parentNode && n.type !== 'comment')
  return (
    leaves.find((n) => n.type === 'operation' || n.type === 'foreach') ?? leaves[0]
  )?.id ?? SOURCE_ID
}

// ── Form specs per operation ──────────────────────────────────────────
// control: how to render the field. The columns come from the schema upstream (the
// node's input), not from free text.
export interface FieldSpec {
  key: string
  label: string
  control:
    | 'column' // single column select
    | 'columns' // column multi-select
    | 'operator'
    | 'func'
    | 'dtype'
    | 'how'
    | 'text'
    | 'number'
    | 'boolean'
    | 'value' // filter value (smart parsing)
    | 'castlist' // {column: dtype}
    | 'renamelist' // {from: to}
    | 'filllist' // {column: value}
    | 'agglist' // [{column, func, alias}]
    | 'exprlist' // [{name, expr}] — computed columns (SQL expressions)
    | 'strategy' // union: relaxed | strict
    | 'joinkeys' // join: left column → right column rows (different names too)
    | 'reorder' // reordering columns with drag-and-drop
    | 'sortlist' // sort keys in order, a direction each (by[] + descending[])
    | 'json' // textarea with free JSON (e.g. the foreach's items)
    | 'sqltext' // textarea with a whole SQL query (Execute SQL); MANUAL preview
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
  sort: [{ key: 'by', label: 'params.sort_by', control: 'sortlist' }],
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
    // several columns = combinations (labels joined with '_', see the cross-engine standard)
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

/** Filter operators that require no value. */
export const NO_VALUE_OPERATORS = new Set(['is_null', 'is_not_null'])

/** Sensible default parameters when an operation is chosen. */
export function defaultParams(opType: string): Record<string, any> {
  switch (opType) {
    case 'filter':
      return { operator: 'eq' }
    case 'join':
      return { how: 'inner' }
    case 'union':
      return { strategy: 'relaxed' }
    case 'foreach':
      return { add_keys_as_columns: true }
    case 'sql':
      return { query: '' }
    default:
      return {}
  }
}
