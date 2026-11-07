// Data APIs: datasources published as a read-only HTTP API for other systems, answered by pods
// of their own on Kubernetes (docs/specs/data-apis.md). The rules are the gateway's
// (gateway-go/internal/dataapis); the helpers below only tell the page what to offer.

export type DataApiState = 'draft' | 'active' | 'paused'
export type DataApiOperator = 'eq' | 'in' | 'gt' | 'gte' | 'lt' | 'lte' | 'null'
export const DATA_API_OPERATORS: DataApiOperator[] = ['eq', 'in', 'gt', 'gte', 'lt', 'lte', 'null']

export interface DataApiRuntime {
  autoscale: boolean
  replicas: number
  min_replicas: number
  max_replicas: number
  cpu: string
  memory: string
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
  max_replicas?: number
  max_total_replicas?: number
  max_cpu?: string
  max_memory?: string
  key_max_days?: number
  key_default_rate?: number
  key_max_rate?: number
  max_grace_days?: number
  default_page?: number
  max_page?: number
  default_runtime?: DataApiRuntime
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
  runtime?: DataApiRuntime
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
