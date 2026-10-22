// Client dell'API dati. Passa dal gateway (che autentica e inoltra all'engine).
// Tutte le chiamate viaggiano col cookie della sessione (HttpOnly) via useApiClient().

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
  // stato della step-cache del passo A MONTE del nodo: hit | pending | skipped
  // (oltre il tetto: ricalcolato a ogni anteprima) | off (Viewer); assente se
  // non c'e' un passo a monte
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

// info di deployment esposte dal gateway (/system/info)
export type AppInfo = {
  name: string
  version: string
  codename?: string
  timezone: string
  // campione automatico di sviluppo (righe; 0/assente = spento): l'editor lo
  // applica ai nodi sorgente senza un campione scelto, come fa il gateway
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

/** Le opzioni del download, come le valida il gateway (`OpzioniDbt`). */
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

  return {
    async operations(): Promise<string[]> {
      return await apiFetch<string[]>('/tasks/operations')
    },

    async uploadFile(file: File, dtypeOverrides?: Record<string, string>): Promise<IngestResult> {
      const fd = new FormData()
      fd.append('file', file)
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
      engine?: string // motore del flusso (polars | duckdb); assente = default
      no_cache?: boolean // Viewer: query esplorative che non sporcano la step-cache
      sort_keys?: string[] // ORDER BY che la copia materializzata (ClickHouse) eredita
      slot?: string // in ogni slot conta solo l'ultima preview (vedi usePreviewSlots)
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

    // catalogo degli engine disponibili (per il picker in creazione flusso)
    async engines(): Promise<{ id: string; label: string; available: boolean; description: string; optional?: boolean }[]> {
      return await apiFetch('/engines')
    },

    // info di deployment (fuso orario degli schedule, nome, versione)
    async appInfo(): Promise<AppInfo> {
      return await apiFetch('/system/info')
    },

    // carico previsionale degli schedule (heatmap giorno×ora + fasce critiche)
    async scheduleLoad(days = 7): Promise<ScheduleLoad> {
      return await apiFetch<ScheduleLoad>(`/schedule/load?days=${days}`)
    },

    // ── Audit log (solo admin) ────────────────────────────────────────────────
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
    // le più recenti, e quante sono in tutto
    async auditSessions(): Promise<Page<ActiveSession>> {
      return await apiFetch<Page<ActiveSession>>('/audit/sessions')
    },
    // l'amministratore chiude la sessione di qualcuno: fuori alla richiesta dopo
    async revokeSession(id: string): Promise<void> {
      await apiFetch(`/audit/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
    // le proprie sessioni
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

    // grafo di lineage: senza center → grafo completo leggibile; con center →
    // sottografo a monte/valle entro `depth` salti
    async lineage(p?: {
      type?: 'flow' | 'datasource' | 'connection' | 'script'
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
      engine?: string // motore che calcola lo snapshot; il file lo scrive Polars
    }): Promise<void> {
      const { task_id } = await apiFetch<{ task_id: string }>('/tasks/exports', { method: 'POST', body })
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
      a.download = body.filename ?? ''
      a.click()
    },

    // esporta un flusso come progetto dbt (zip). target: 'duckdb' (federato) |
    // 'native' (warehouse di origine, SQL tradotto via sqlglot)
    /** Cosa serve al dialogo dell'export dbt: target possibili, sorgenti, uscite (solo admin). */
    async dbtExportPlan(flowId: number, opts: { signal?: AbortSignal } = {}): Promise<DbtExportPlan> {
      return await apiFetch<DbtExportPlan>(`/flows/${flowId}/export/dbt/plan`, { signal: opts.signal })
    },
    /** Il progetto dbt (zip) con le scelte del dialogo. With it, the file name the gateway gives
     *  it: null when the Content-Disposition cannot be read (cross-origin, not exposed). */
    async exportFlowDbt(flowId: number, options: DbtExportOptions, opts: { signal?: AbortSignal } = {}): Promise<{ blob: Blob; filename: string | null }> {
      let disposition: string | null = null
      const blob = await apiFetch<Blob>(`/flows/${flowId}/export/dbt`, {
        method: 'POST', body: options, responseType: 'blob', signal: opts.signal,
        onResponse: ({ response }: { response: Response }) => { disposition = response.headers.get('content-disposition') },
      })
      return { blob, filename: fileNameFromDisposition(disposition) }
    },
    /** Le dipendenze del flusso come eventi OpenLineage (JSON), ricostruite dalla definizione. */
    async exportFlowOpenLineage(flowId: number): Promise<unknown[]> {
      return await apiFetch<unknown[]>(`/flows/${flowId}/openlineage`)
    },
  }
}

// ── Lineage cross-flow ───────────────────────────────────────────────────────
export type LineageNodeType = 'flow' | 'datasource' | 'connection' | 'script' | 'db_sink' | 's3_sink' | 'sftp_sink'
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

// ── Carico previsionale degli schedule ───────────────────────────────────────
export interface ScheduleLoadCell {
  weekday: number // 0 = lunedì … 6 = domenica
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
// Una sessione aperta (un browser, uno script): la vede chi la possiede in
// «Le tue sessioni»; l'amministratore vede quelle di tutti nell'Audit.
export interface MySession {
  id: string
  created_at: string
  last_seen_at: string
  expires_at: string
  ip: string | null
  user_agent: string | null
  method: 'password' | 'sso' | string
  current: boolean // quella da cui si guarda
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

// Una pagina di risultati dalle liste paginate (ricerca server-side).
export interface Page<T> {
  items: T[]
  total: number
}

// Query-string per gli endpoint paginati /…/search
export function pagedQuery(p: { q?: string; limit: number; offset: number }): string {
  const qs = new URLSearchParams()
  if (p.q) qs.set('q', p.q)
  qs.set('limit', String(p.limit))
  qs.set('offset', String(p.offset))
  return qs.toString()
}

// Estrae un messaggio d'errore leggibile da un errore di $fetch.
export function errMessage(e: any): string {
  return e?.data?.detail ?? e?.data?.message ?? e?.message ?? (clientTranslate?.('errors.unknown') || 'Unknown error')
}
