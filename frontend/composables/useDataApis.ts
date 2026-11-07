// Data APIs: datasources published as a read-only HTTP API for other systems, answered by pods
// of their own on Kubernetes (docs/specs/data-apis.md). The rules are the gateway's
// (gateway-go/internal/dataapis); the helpers below only tell the page what to offer.

export type DataApiState = 'draft' | 'active' | 'paused'
export type DataApiOperator = 'eq' | 'in' | 'gt' | 'gte' | 'lt' | 'lte' | 'null'
export const DATA_API_OPERATORS: DataApiOperator[] = ['eq', 'in', 'gt', 'gte', 'lt', 'lte', 'null']

/** How a data API's pods run. Only an administrator sets it (Administration → Data APIs, or the
 *  page when an administrator opens it); a new data API and a copy start from the default. */
export interface DataApiRuntime {
  autoscale: boolean
  replicas: number
  min_replicas: number
  max_replicas: number
  cpu: string
  memory: string
}

/** The bounds of a runtime (dataapis.CheckRuntime): not the chart's, only what makes sense. */
export interface DataApiRuntimeLimits {
  min_cpu: string
  max_cpu: string
  min_memory: string
  max_memory: string
  /** the most pods of one data API: 40, or DATA_API__MAX_TOTAL_REPLICAS when fewer */
  max_replicas: number
}

export interface DataApiFilter {
  column: string
  operators: DataApiOperator[]
}

export interface DataApiResource {
  name: string
  source: { kind: 'datasource'; id: number }
  columns: string[]
  filters: DataApiFilter[]
  /** columns among the filterable ones, «-» in front for descending */
  default_sort: string[]
  max_page: number
}

export interface DataApiPerson { id: number; email: string; full_name: string }

export interface DataApi {
  id: number
  name: string
  slug: string
  /** /data/{slug}/ */
  path: string
  description: string
  project_id: number
  state: DataApiState
  current_version: number
  resources: number
  creator: DataApiPerson | null
  created_at: string | null
  updated_at: string | null
  /** MANAGE on its folder, or an administrator: editing and the keys */
  can_manage: boolean
  /** an administrator (not an observer): the runtime */
  can_set_runtime: boolean
}

export interface DataApiResourceOut extends DataApiResource {
  /** its name and columns only for whoever may read it */
  datasource: { id: number; name: string | null; readable: boolean; exists: boolean; columns: { name: string; type: string }[] }
}

export interface DataApiDetail extends DataApi {
  runtime: DataApiRuntime
  resource_list: DataApiResourceOut[]
  /** why it would not answer on its creator's authority now; '' when nothing stops it */
  suspended: '' | 'creator_inactive' | 'creator_rights' | 'datasource_rights'
}

export interface DataApiLimits {
  enabled: boolean
  /** why not: 'kubernetes' (Docker Compose) or 'disabled' (DATA_API__ENABLED) */
  reason: '' | 'kubernetes' | 'disabled'
  count?: number
  max_apis?: number
  max_total_replicas?: number
  runtime_limits?: DataApiRuntimeLimits
  key_max_days?: number
  key_default_rate?: number
  key_max_rate?: number
  max_grace_days?: number
  default_page?: number
  max_page?: number
  default_runtime?: DataApiRuntime
  /** the address the other systems call (dataApi.publicHost), '' for the interface's own */
  base_url?: string
  /** the host the data APIs are published on, '' for the interface's own */
  host?: string
}

export type DataApiKeyState = 'active' | 'grace' | 'replaced' | 'expired' | 'revoked'

export interface DataApiKey {
  id: number
  name: string
  /** the key begins with tdk_ and these 8 characters */
  hint: string
  state: DataApiKeyState
  expires_at: string | null
  rate_per_minute: number
  /** [] = every resource */
  resources: string[]
  /** [] = from any address */
  cidrs: string[]
  revoked_at: string | null
  grace_until: string | null
  created_by: DataApiPerson | null
  created_at: string | null
  last_used_at: string | null
  requests_24h: number
}

export interface DataApiVersion {
  version: number
  note: string
  created_at: string | null
  created_by: string | null
  current: boolean
}

export interface DataApiSave {
  name?: string
  slug?: string
  description?: string
  project_id?: number
  resources?: DataApiResource[]
}

/** One pod of a data API, as the cluster sees it and as it reports itself. */
export type DataApiPodCondition = 'ready' | 'preparing' | 'starting' | 'error' | 'stopping'
export interface DataApiPodResource {
  name: string
  /** the data version it serves, absent when none yet */
  version?: string
  /** the version it is preparing */
  preparing?: string
  /** why the latest version did not prepare (tried again later) */
  error?: string
  bytes?: number
}
export interface DataApiPod {
  name: string
  condition: DataApiPodCondition
  phase: string
  ready: boolean
  restarts: number
  node: string
  started: string | null
  reason: string
  message: string
  terminating: boolean
  reported_at: string | null
  state: string
  error: string
  resources: DataApiPodResource[]
}
export interface DataApiUsagePoint { at: string; requests: number; rows: number; errors: number; throttled: number }
export interface DataApiStatus {
  state: DataApiState
  /** when the reconcile loop last looked; null: not yet */
  checked_at: string | null
  wanted: number
  ready: number
  updated: number
  disk: string
  error: string
  pods: DataApiPod[]
  /** each resource: the version its datasource has now, and how many pods serve it */
  resources: { name: string; latest: string; serving: number }[]
  traffic: { hours: number; bucket_minutes: number; points: DataApiUsagePoint[]; totals: Omit<DataApiUsagePoint, 'at'> }
  poll_seconds: number
  path: string
  host: string
  /** the address the other systems call (dataApi.publicHost); '' for the interface's own */
  base_url: string
}

/** The kind a data API gives a column: how a filter reads its value, how an answer writes it. */
export type DataApiKind = 'integer' | 'float' | 'decimal' | 'boolean' | 'string' | 'date' | 'timestamp' | 'timestamptz'
  | 'time' | 'uuid' | 'other'

/** GET /data-apis/{id}/documentation: what the page documents, with the pods' own description. */
export interface DataApiDoc {
  state: DataApiState
  base_url: string
  host: string
  path: string
  version: number
  resources: {
    name: string
    path: string
    /** kind '' : the datasource no longer has the column */
    columns: { name: string; kind: DataApiKind | '' }[]
    filters: DataApiFilter[]
    default_sort: string[]
    max_page: number
  }[]
  max_in_values: number
  max_sort_keys: number
  errors: { status: number; code: string; meaning: string }[]
  openapi: Record<string, any>
}

/** A data API as the administration lists it. */
export interface DataApiAdminRow {
  id: number
  name: string
  slug: string
  path: string
  project_id: number
  state: DataApiState
  runtime: DataApiRuntime
  creator: (DataApiPerson & { active: boolean }) | null
  resources: number
  keys: number
  created_at: string | null
  updated_at: string | null
  wanted: number
  ready: number
  error: string
  checked_at: string | null
  requests_24h: number
  errors_24h: number
  throttled_24h: number
  last_used_at: string | null
}
export interface DataApiAdminList {
  enabled: boolean
  reason: '' | 'kubernetes' | 'disabled'
  max_apis: number
  max_total_replicas: number
  runtime_limits: DataApiRuntimeLimits
  default_runtime: DataApiRuntime
  base_url: string
  host: string
  apis: DataApiAdminRow[]
}

// ── the runtime ──────────────────────────────────────────────────────────────

/** A CPU quantity («500m», «2», «1.5») in thousandths of a core; null when it is not one. */
export function millicores(q: string): number | null {
  const m = /^([0-9]+(?:\.[0-9]+)?)(m?)$/.exec(q.trim())
  if (!m) return null
  const f = Number(m[1])
  return Math.round(m[2] ? f : f * 1000)
}

const MEMORY_UNITS: Record<string, number> = { '': 1, K: 1e3, M: 1e6, G: 1e9, T: 1e12, Ki: 2 ** 10, Mi: 2 ** 20, Gi: 2 ** 30, Ti: 2 ** 40 }

/** A memory quantity («512Mi», «2Gi», «1G») in bytes; null when it is not one. */
export function memoryBytes(q: string): number | null {
  const m = /^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?$/.exec(q.trim())
  if (!m) return null
  return Math.floor(Number(m[1]) * MEMORY_UNITS[m[2] ?? '']!)
}

/** The most pods the runtime may run. */
export function runtimePods(rt: DataApiRuntime): number {
  return rt.autoscale ? rt.max_replicas : rt.replicas
}

/** Whether two runtimes run the same pods (the gateway's SameRuntime). */
export function sameRuntime(a: DataApiRuntime, b: DataApiRuntime): boolean {
  if (a.autoscale !== b.autoscale) return false
  if (a.autoscale ? a.min_replicas !== b.min_replicas || a.max_replicas !== b.max_replicas : a.replicas !== b.replicas) return false
  const ca = millicores(a.cpu), cb = millicores(b.cpu), ma = memoryBytes(a.memory), mb = memoryBytes(b.memory)
  return (ca == null || cb == null ? a.cpu.trim() === b.cpu.trim() : ca === cb)
    && (ma == null || mb == null ? a.memory.trim() === b.memory.trim() : ma === mb)
}

/** What is wrong with a runtime before the gateway says it: an i18n key of dataApi and its
 *  values, null when nothing. The gateway checks the same (and the pods of all the active ones). */
export function runtimeProblem(rt: DataApiRuntime, l: DataApiRuntimeLimits): { key: string; values?: Record<string, string | number> } | null {
  const whole = (n: number) => Number.isInteger(n) && n >= 1
  if (rt.autoscale) {
    if (!whole(rt.min_replicas) || !whole(rt.max_replicas) || rt.max_replicas < rt.min_replicas) return { key: 'rtBadRange' }
  } else if (!whole(rt.replicas)) return { key: 'rtBadReplicas' }
  if (runtimePods(rt) > l.max_replicas) return { key: 'rtTooManyPods', values: { n: l.max_replicas } }
  const cpu = millicores(rt.cpu)
  if (cpu == null) return { key: 'rtBadCpu' }
  if (cpu < millicores(l.min_cpu)! || cpu > millicores(l.max_cpu)!) return { key: 'rtCpuRange', values: { min: l.min_cpu, max: l.max_cpu } }
  const mem = memoryBytes(rt.memory)
  if (mem == null) return { key: 'rtBadMemory' }
  if (mem < memoryBytes(l.min_memory)! || mem > memoryBytes(l.max_memory)!) {
    return { key: 'rtMemoryRange', values: { min: l.min_memory, max: l.max_memory } }
  }
  return null
}

/** The runtime tidied as the gateway keeps it: the unused numbers in line with the used ones. */
export function tidyRuntime(rt: DataApiRuntime): DataApiRuntime {
  const out = { ...rt, cpu: rt.cpu.trim(), memory: rt.memory.trim() }
  if (out.autoscale) out.replicas = out.min_replicas
  else out.min_replicas = out.max_replicas = out.replicas
  return out
}

/** Where the other systems call a data API: its public address, the host it is published on,
 *  or the page's own origin. */
export function dataApiOrigin(baseUrl: string, host: string, here: { protocol: string; origin: string }): string {
  if (baseUrl) return baseUrl.replace(/\/+$/, '')
  if (host) return `${here.protocol}//${host}`
  return here.origin
}

/** A value of this kind to show in an example: one a filter reads. */
export function sampleValue(kind: DataApiKind | ''): string {
  switch (kind) {
    case 'integer': return '42'
    case 'float': case 'decimal': return '12.50'
    case 'boolean': return 'true'
    case 'date': return '2026-01-31'
    case 'timestamp': return '2026-01-31T08:30:00'
    case 'timestamptz': return '2026-01-31T08:30:00Z'
    case 'time': return '08:30:00'
    case 'uuid': return '123e4567-e89b-12d3-a456-426614174000'
  }
  return 'text'
}

/** The query-string parameter of a filter's operator: col, col__in, col__gte… */
export function filterParam(column: string, op: DataApiOperator): string {
  return op === 'eq' ? column : `${column}__${op}`
}

/** A curl command line for a resource, with the key in a variable (never written here). */
export function curlLine(url: string, query: Record<string, string> = {}, header: 'bearer' | 'apikey' = 'bearer'): string {
  const qs = Object.entries(query).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v).replace(/%2C/g, ',')}`).join('&')
  const auth = header === 'bearer' ? '-H "Authorization: Bearer $TABULARIA_KEY"' : '-H "X-API-Key: $TABULARIA_KEY"'
  return `curl ${auth} "${url}${qs ? `?${qs}` : ''}"`
}

/** The OpenAPI document to download: the gateway's, with the address it is called at when the
 *  gateway wrote it relative («/»: the page's own). */
export function openapiForDownload(doc: DataApiDoc, origin: string): Record<string, any> {
  const out = JSON.parse(JSON.stringify(doc.openapi))
  if (!doc.base_url) out.servers = [{ url: origin }]
  return out
}

export function useDataApis() {
  const { apiFetch } = useApiClient()
  return {
    limits: () => apiFetch<DataApiLimits>('/data-apis/limits'),
    list: () => apiFetch<DataApi[]>('/data-apis'),
    listByProject: (projectId: number) => apiFetch<DataApi[]>(`/projects/${projectId}/data-apis`),
    get: (id: number) => apiFetch<DataApiDetail>(`/data-apis/${id}`),
    create: (projectId: number, body: DataApiSave) =>
      apiFetch<DataApiDetail>(`/projects/${projectId}/data-apis`, { method: 'POST', body }),
    update: (id: number, body: DataApiSave) => apiFetch<DataApiDetail>(`/data-apis/${id}`, { method: 'PATCH', body }),
    duplicate: (id: number) => apiFetch<DataApiDetail>(`/data-apis/${id}/duplicate`, { method: 'POST' }),
    remove: (id: number) => apiFetch<void>(`/data-apis/${id}`, { method: 'DELETE' }),
    activate: (id: number) => apiFetch<DataApiDetail>(`/data-apis/${id}/activate`, { method: 'POST' }),
    pause: (id: number) => apiFetch<DataApiDetail>(`/data-apis/${id}/pause`, { method: 'POST' }),
    status: (id: number, hours = 24) => apiFetch<DataApiStatus>(`/data-apis/${id}/status?hours=${hours}`),
    documentation: (id: number) => apiFetch<DataApiDoc>(`/data-apis/${id}/documentation`),
    adminList: () => apiFetch<DataApiAdminList>('/admin/data-apis'),
    adminPause: (id: number) => apiFetch<void>(`/admin/data-apis/${id}/pause`, { method: 'POST' }),
    adminSetRuntime: (id: number, runtime: DataApiRuntime) =>
      apiFetch<DataApiRuntime>(`/admin/data-apis/${id}/runtime`, { method: 'PUT', body: runtime }),
    adminRemove: (id: number) => apiFetch<void>(`/admin/data-apis/${id}`, { method: 'DELETE' }),
    versions: (id: number) => apiFetch<DataApiVersion[]>(`/data-apis/${id}/versions`),
    restore: (id: number, version: number) =>
      apiFetch<DataApiDetail>(`/data-apis/${id}/versions/${version}/restore`, { method: 'POST' }),
    keys: (id: number) => apiFetch<DataApiKey[]>(`/data-apis/${id}/keys`),
    createKey: (id: number, body: { name: string; expires_at: string; rate_per_minute?: number; resources: string[]; cidrs: string[] }) =>
      apiFetch<{ key: DataApiKey; secret: string }>(`/data-apis/${id}/keys`, { method: 'POST', body }),
    revokeKey: (id: number, keyId: number) => apiFetch<void>(`/data-apis/${id}/keys/${keyId}/revoke`, { method: 'POST' }),
    renewKey: (id: number, keyId: number, body: { expires_at: string; grace_hours: number }) =>
      apiFetch<{ key: DataApiKey; secret: string; replaced: DataApiKey }>(`/data-apis/${id}/keys/${keyId}/renew`, { method: 'POST', body }),
  }
}

// ── what the page offers ─────────────────────────────────────────────────────

/** The operators a column's type allows (Polars' names, as the datasources list them): every
 * scalar column equality, a list and «is null»; numbers and dates the comparisons too; lists,
 * structures and binaries none. The gateway decides the same. */
export function operatorsFor(type: string): DataApiOperator[] {
  const kind = kindOfPolars(type)
  if (kind === 'other') return []
  if (['integer', 'float', 'decimal', 'date', 'timestamp', 'timestamptz', 'time'].includes(kind)) return [...DATA_API_OPERATORS]
  return ['eq', 'in', 'null']
}

/** The kind a data API gives a datasource's column, from its Polars type: the gateway's
 *  dataapiwire.KindOfPolars, which the pods agree with once they read the parquet. */
export function kindOfPolars(type: string): DataApiKind {
  const t = type.trim()
  const name = t.split('(')[0]!
  if (['Int8', 'Int16', 'Int32', 'Int64', 'UInt8', 'UInt16', 'UInt32', 'UInt64', 'Duration'].includes(name)) return 'integer'
  if (name === 'Float32' || name === 'Float64') return 'float'
  if (name === 'Decimal') return 'decimal'
  if (name === 'Boolean') return 'boolean'
  if (['String', 'Utf8', 'Categorical', 'Enum'].includes(name)) return 'string'
  if (name === 'Date') return 'date'
  if (name === 'Datetime') return /time_zone=/.test(t) && !/time_zone=None/.test(t) ? 'timestamptz' : 'timestamp'
  if (name === 'Time') return 'time'
  return 'other'
}

/** A resource's name from a datasource's: lowercase letters, digits and dashes, at most 63. */
export function resourceName(name: string): string {
  const s = name.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 63).replace(/-+$/, '')
  return s || 'resource'
}

/** A data API's address from its name: the same rule. */
export const slugOf = resourceName

/** A valid address or resource name: what the gateway accepts. */
export function validLabel(s: string): boolean {
  return /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(s)
}

/** A resource for a datasource just chosen: every column goes out, the first one filtered on by
 * equality and lists (the key a CRM asks by) and sorted by. */
export function newResource(ds: { id: number; name: string; columns: { name: string; type: string }[] }, taken: string[],
  page = 1000): DataApiResource {
  let name = resourceName(ds.name)
  for (let i = 2; taken.includes(name); i++) name = `${resourceName(ds.name).slice(0, 59)}-${i}`
  const first = ds.columns.find((c) => operatorsFor(c.type).length)
  return {
    name, source: { kind: 'datasource', id: ds.id }, columns: ds.columns.map((c) => c.name),
    filters: first ? [{ column: first.name, operators: ['eq', 'in'] }] : [],
    default_sort: first ? [first.name] : [], max_page: page,
  }
}

/** What a resource still sends that its datasource no longer has, or would refuse: columns gone,
 * filters and the order on columns that do not go out. The page cleans them before saving. */
export function tidyResource(r: DataApiResource): DataApiResource {
  const out = new Set(r.columns)
  const filters = r.filters.filter((f) => out.has(f.column) && f.operators.length)
  const filtered = new Set(filters.map((f) => f.column))
  return { ...r, filters, default_sort: r.default_sort.filter((s) => filtered.has(s.replace(/^-/, ''))) }
}

/** The key's state as the list says it, and whether it still answers. */
export function keyAnswers(state: DataApiKeyState): boolean {
  return state === 'active' || state === 'grace'
}

/** Addresses typed one per line or separated by commas. */
export function cidrsOf(text: string): string[] {
  return text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean)
}
