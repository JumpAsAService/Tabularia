// The data API's client. It goes through the gateway (which authenticates and forwards
// to the engine). Every call travels with the session's cookie (HttpOnly) through
// useApiClient().

import { clientTranslate } from '~/plugins/i18n'

export interface ColumnInfo {
  name: string
  dtype: string
}

export interface PreviewResult {
  columns: ColumnInfo[]
  rows: Record<string, any>[]
  row_count: number
  truncated: boolean
  // step cache state of the node's UPSTREAM step: hit | pending | skipped (over the cap:
  // recomputed at every preview) | off (Viewer); missing when there is no upstream step
  cache_state?: 'hit' | 'pending' | 'skipped' | 'off' | null
  cache_cap_rows?: number | null
}

export interface IngestResult {
  dataset_id: string
  status: 'ready' | 'processing'
  parquet_key: string
  raw_key: string
  format: string
  size_bytes: number
  dataset?: { dataset_id: string; rows: number; columns: ColumnInfo[] }
  task_id?: string
  // a workbook: its sheets in order, the hidden ones, the one read
  sheets?: string[] | null
  hidden_sheets?: string[] | null
  sheet?: string | null
}

export interface TaskResponse {
  task_id: string
  status: string
  message: string
}

export interface TaskStatus {
  task_id: string
  status: string
  result?: any
  error?: string
  message?: string
}

/** The state of an export a worker is writing (see `exportData`). */
export interface ExportStatus {
  status: 'running' | 'ready' | 'failed'
  filename?: string
  size?: number
  code?: number
  detail?: string
}

export interface Operation {
  type: string
  params: Record<string, any>
}

// deployment info exposed by the gateway (/system/info)
export type AppInfo = {
  name: string
  version: string
  codename?: string
  timezone: string
  // automatic development sample (rows; 0/missing = off): the editor applies it to the
  // source nodes without a chosen sample, as the gateway does
  preview_default_sample_rows?: number
}

export type DbtTarget = 'clickhouse' | 'duckdb' | 'native'
export type DbtMaterialization = 'table' | 'view' | 'incremental'

export interface DbtExportPlan {
  flow: string
  default_folder: string
  targets: {
    id: DbtTarget
    available: boolean
    reason: string | null
    /** default_name: the source's name with nothing typed; default_base: the default it was made
     *  from before a collision moved it aside (`_2`, `_3`), which the gateway suffixes again from
     *  when typed names change the collisions (absent from older gateways) */
    sources: { key: string; connection: string; schema: string; default_name: string; default_base?: string; tables: string[] }[]
    /** A target that is not available as such but takes the flow into the team's own project
     *  (folder package) when these sources are already declared there: ClickHouse with an
     *  Oracle source. `reason` still says why it is off in the complete project. */
    requires?: { package: 'folder'; declared: string[] }
  }[]
  outputs: { key: string; model: string; flow: string; label: string | null; materialized: DbtMaterialization; materializations: DbtMaterialization[] }[]
  /** available: the AI assistant is configured and a model is enabled. stored: what the AI wrote
   *  for the flow's CURRENT version (non-empty texts only) and the models that wrote it: a
   *  re-export reuses it without any AI call, also with the AI off (absent from older gateways). */
  ai?: { available: boolean; model: string | null; stored?: { descriptions: boolean; translations: boolean; models: string[] } }
}

/** The download's options, as the gateway validates them (`DbtOptions`). */
export interface DbtExportOptions {
  target: DbtTarget
  package: 'project' | 'folder'
  folder: string
  prefix: string
  layers: boolean
  sources: Record<string, { name: string | null; declared: boolean }>
  schema?: string
  materializations: Record<string, DbtMaterialization>
  tests: boolean
  emails: boolean
  ai_descriptions?: boolean
  ai_translations?: boolean
}

/** The name of a downloaded file from its Content-Disposition: the RFC 5987 `filename*`
 *  (UTF-8, so accented and non-Latin letters survive), or null without one. The plain
 *  `filename` is the gateway's ASCII fallback, which drops those letters: not used. */
export function fileNameFromDisposition(disposition: string | null | undefined): string | null {
  const m = disposition?.match(/filename\*\s*=\s*UTF-8''([^;\s]+)/i)
  if (!m) return null
  try {
    // only a name, never a path
    return decodeURIComponent(m[1].replace(/^"|"$/g, '')).replace(/[\\/]/g, '_') || null
  } catch {
    return null
  }
}

export function useApi() {
  const { apiFetch } = useApiClient()
  const base = useRuntimeConfig().public.apiBase as string

  // follows an export started already (the Viewer's, a SQL script's) and, once it is
  // ready, has the browser save its file
  async function followExport(task_id: string, filename?: string): Promise<void> {
    let failures = 0
    for (let wait = 400; ; wait = Math.min(wait * 2, 2000)) {
      await new Promise((resolve) => setTimeout(resolve, wait))
      let state: ExportStatus
      try {
        state = await apiFetch<ExportStatus>(`/tasks/exports/${task_id}`)
        failures = 0
      } catch (e: any) {
        // a network blip does not lose an export that is still being written
        const code = e?.response?.status ?? e?.statusCode
        if ((code && code < 500) || ++failures >= 5) throw e
        continue
      }
      if (state.status === 'failed') {
        throw Object.assign(new Error(state.detail ?? 'Export failed'), { data: { detail: state.detail }, statusCode: state.code })
      }
      if (state.status === 'ready') break
    }
    const a = document.createElement('a')
    a.href = `${base}/tasks/exports/${task_id}/file`
    a.download = filename ?? ''
    a.click()
  }

  return {
    async operations(): Promise<string[]> {
      return await apiFetch<string[]>('/tasks/operations')
    },

    async uploadFile(file: File, dtypeOverrides?: Record<string, string>, sheet?: string): Promise<IngestResult> {
      const fd = new FormData()
      fd.append('file', file)
      if (sheet) fd.append('sheet', sheet) // a workbook: this sheet (none = the first visible one)
      if (dtypeOverrides && Object.keys(dtypeOverrides).length) {
        fd.append('dtype_overrides', JSON.stringify(dtypeOverrides))
      }
      return await apiFetch<IngestResult>('/files', { method: 'POST', body: fd })
    },

    async preview(body: {
      bucket: string
      input_key: string
      operations: Operation[]
      limit?: number
      engine?: string // the flow's engine (polars | duckdb); missing = default
      no_cache?: boolean // Viewer: exploratory queries that do not dirty the step cache
      sort_keys?: string[] // ORDER BY the materialized copy (ClickHouse) inherits
      slot?: string // in every slot only the last preview counts (see usePreviewSlots)
    }, opts: { signal?: AbortSignal } = {}): Promise<PreviewResult> {
      return await apiFetch<PreviewResult>('/tasks/preview', { method: 'POST', body, signal: opts.signal })
    },

    async transform(body: {
      bucket: string
      input_key: string
      output_key: string
      operations: Operation[]
      engine?: string
    }): Promise<TaskResponse> {
      return await apiFetch<TaskResponse>('/tasks/transform-data', { method: 'POST', body })
    },

    // catalog of the available engines (for the picker at flow creation)
    async engines(): Promise<{ id: string; label: string; available: boolean; description: string; optional?: boolean }[]> {
      return await apiFetch('/engines')
    },

    // deployment info (the schedules' time zone, name, version)
    async appInfo(): Promise<AppInfo> {
      return await apiFetch('/system/info')
    },

    // the schedules' forecast load (day×hour heatmap + critical slots)
    async scheduleLoad(days = 7): Promise<ScheduleLoad> {
      return await apiFetch<ScheduleLoad>(`/schedule/load?days=${days}`)
    },

    // ── Audit log (admin only) ────────────────────────────────────────────────
    async audit(p: {
      q?: string; action?: string; outcome?: string; target_type?: string
      limit: number; offset: number
    }): Promise<Page<AuditEntry>> {
      const s = new URLSearchParams()
      if (p.q) s.set('q', p.q)
      if (p.action) s.set('action', p.action)
      if (p.outcome) s.set('outcome', p.outcome)
      if (p.target_type) s.set('target_type', p.target_type)
      s.set('limit', String(p.limit))
      s.set('offset', String(p.offset))
      return await apiFetch<Page<AuditEntry>>(`/audit?${s.toString()}`)
    },
    async auditActions(): Promise<string[]> {
      return await apiFetch<string[]>('/audit/actions')
    },
    // the newest, and how many there are in all
    async auditSessions(): Promise<Page<ActiveSession>> {
      return await apiFetch<Page<ActiveSession>>('/audit/sessions')
    },
    // the administrator closes someone's session: out at the next request
    async revokeSession(id: string): Promise<void> {
      await apiFetch(`/audit/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
    // one's own sessions
    async mySessions(): Promise<MySession[]> {
      return await apiFetch<MySession[]>('/auth/sessions')
    },
    async closeMySession(id: string): Promise<void> {
      await apiFetch(`/auth/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
    async closeOtherSessions(): Promise<void> {
      await apiFetch('/auth/sessions/close-others', { method: 'POST' })
    },
    // one's own password: the gateway checks the current one and closes the other sessions
    async changeMyPassword(current_password: string, new_password: string): Promise<void> {
      await apiFetch('/users/me/password', { method: 'POST', body: { current_password, new_password } })
    },
    async auditAccessActivity(hours = 24): Promise<AccessActivity> {
      return await apiFetch<AccessActivity>(`/audit/access-activity?hours=${hours}`)
    },

    // lineage graph: without center → the whole readable graph; with center → the subgraph
    // upstream/downstream within `depth` hops
    async lineage(p?: {
      type?: 'flow' | 'datasource' | 'connection' | 'script' | 'sql' | 'notebook' | 'group'
      id?: number
      direction?: 'both' | 'upstream' | 'downstream'
      depth?: number
    }): Promise<LineageGraph> {
      const q = new URLSearchParams()
      if (p?.type && p?.id != null) {
        q.set('type', p.type)
        q.set('id', String(p.id))
      }
      if (p?.direction) q.set('direction', p.direction)
      if (p?.depth) q.set('depth', String(p.depth))
      const qs = q.toString()
      return await apiFetch<LineageGraph>(`/lineage${qs ? `?${qs}` : ''}`)
    },

    /** Which nodes of a flow have their result in the step cache, for the engine: the
     *  chains are the ones a preview of each node would send. */
    async cacheStatus(body: {
      engine?: string
      chains: { node_id: string; bucket: string; input_key: string; operations: Operation[] }[]
    }): Promise<{ cached: string[]; too_big: string[] }> {
      return await apiFetch('/tasks/cache-status', { method: 'POST', body })
    },

    async taskStatus(id: string): Promise<TaskStatus> {
      return await apiFetch<TaskStatus>(`/tasks/${id}`)
    },

    // The result of a chain (even up to an intermediate node) as a csv/xlsx file.
    // A worker writes it; this follows it and, once it is ready, hands its address
    // to the browser, which saves the file as it arrives instead of holding it all
    // in memory. The file can be taken once: it is deleted as soon as it is sent.
    async exportData(body: {
      bucket: string
      input_key: string
      operations: Operation[]
      format: 'csv' | 'xlsx'
      limit?: number
      filename?: string
      engine?: string // the engine computing the snapshot; Polars writes the file
    }): Promise<void> {
      const { task_id } = await apiFetch<{ task_id: string }>('/tasks/exports', { method: 'POST', body })
      await followExport(task_id, body.filename)
    },

    followExport,

    // exports a flow as a dbt project (zip). target: 'duckdb' (federated) | 'native' (the
    // origin warehouse, SQL translated through sqlglot)
    /** What the dbt export's dialog needs: possible targets, sources, outputs (admin only). */
    async dbtExportPlan(flowId: number, opts: { signal?: AbortSignal } = {}): Promise<DbtExportPlan> {
      return await apiFetch<DbtExportPlan>(`/flows/${flowId}/export/dbt/plan`, { signal: opts.signal })
    },
    /**
     * The dbt project (zip) with the dialog's choices. With it, the file name the gateway gives
     * it: null when the Content-Disposition cannot be read (cross-origin, not exposed).
     */
    async exportFlowDbt(flowId: number, options: DbtExportOptions, opts: { signal?: AbortSignal } = {}): Promise<{ blob: Blob; filename: string | null }> {
      let disposition: string | null = null
      const blob = await apiFetch<Blob>(`/flows/${flowId}/export/dbt`, {
        method: 'POST', body: options, responseType: 'blob', signal: opts.signal,
        onResponse: ({ response }: { response: Response }) => { disposition = response.headers.get('content-disposition') },
      })
      return { blob, filename: fileNameFromDisposition(disposition) }
    },
    /** The flow's dependencies as OpenLineage events (JSON), rebuilt from the definition. */
    async exportFlowOpenLineage(flowId: number): Promise<unknown[]> {
      return await apiFetch<unknown[]>(`/flows/${flowId}/openlineage`)
    },
  }
}

// ── Lineage cross-flow ───────────────────────────────────────────────────────
export type LineageNodeType =
  | 'flow' | 'datasource' | 'connection' | 'script' | 'sql_script' | 'notebook' | 'flow_group' | 'container'
  | 'db_sink' | 's3_sink' | 'sftp_sink' | 'email_sink'
export type LineageEdgeKind = 'read' | 'publish' | 'ingest' | 'write' | 'refresh' | 'orchestrate'
export interface LineageNode {
  id: string
  type: LineageNodeType
  label: string
  project_id?: number | null
  kind?: string | null // datasource: flow | database
  restricted?: boolean
  meta?: Record<string, any>
}
export interface LineageEdge {
  source: string
  target: string
  kind: LineageEdgeKind
}
export interface LineageGraph {
  nodes: LineageNode[]
  edges: LineageEdge[]
  center?: string | null
}

// ── The schedules' forecast load ───────────────────────────────────────
export interface ScheduleLoadCell {
  weekday: number // 0 = Monday … 6 = Sunday
  hour: number // 0–23
  count: number
  peak_concurrent: number
  critical: boolean
}
export interface ScheduleCollision {
  weekday: number
  hour: number
  minute: number
  count: number
  queued: number
  schedules: string[]
}
export interface ScheduleLoad {
  days: number
  timezone: string
  worker_capacity: number
  total_schedules: number
  total_firings: number
  cells: ScheduleLoadCell[]
  collisions: ScheduleCollision[]
}

// ── Audit log ─────────────────────────────────────────────────────────────
export interface AuditEntry {
  id: number
  ts: string
  actor_id: number | null
  actor_label: string
  action: string
  outcome: string
  target_type: string | null
  target_id: number | null
  target_label: string | null
  detail: Record<string, any> | null
  ip: string | null
  user_agent: string | null
}
// An open session (a browser, a script): whoever owns it sees it in «Your sessions»; the
// administrator sees everyone's in the Audit.
export interface MySession {
  id: string
  created_at: string
  last_seen_at: string
  expires_at: string
  ip: string | null
  user_agent: string | null
  method: 'password' | 'sso' | string
  current: boolean // the one being looked from
}
export interface ActiveSession extends MySession {
  user_id: number
  email: string
  full_name: string
  is_superuser: boolean
  online: boolean
}
export interface AccessBucket {
  ts: string
  label: string
  success: number
  failure: number
}
export interface AccessActivity {
  hours: number
  timezone: string
  buckets: AccessBucket[]
  total_success: number
  total_failure: number
}

// A page of results from the paginated lists (server-side search).
export interface Page<T> {
  items: T[]
  total: number
}

// Query string for the paginated /…/search endpoints
export function pagedQuery(p: { q?: string; limit: number; offset: number }): string {
  const qs = new URLSearchParams()
  if (p.q) qs.set('q', p.q)
  qs.set('limit', String(p.limit))
  qs.set('offset', String(p.offset))
  return qs.toString()
}

// Extracts a readable error message from a $fetch error. A `detail` that is not text
// is never shown as JSON: FastAPI's validation list gives its messages, with the last part of
// where; an object (a 409 with what uses the thing) gives its `message`.
export function errMessage(e: any): string {
  const detail = e?.data?.detail
  if (Array.isArray(detail)) {
    const lines = detail.map((d: any) => {
      const where = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => typeof x === 'string').slice(-1)[0] : ''
      return typeof d?.msg === 'string' ? (where ? `${where}: ${d.msg}` : d.msg) : ''
    }).filter(Boolean)
    if (lines.length) return lines.join('; ')
  } else if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string') return detail.message
  } else if (typeof detail === 'string' && detail) {
    return detail
  }
  return e?.data?.message ?? e?.message ?? (clientTranslate?.('errors.unknown') || 'Unknown error')
}
