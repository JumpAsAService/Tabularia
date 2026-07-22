// Client dell'API dati. Passa dal gateway (che autentica e inoltra all'engine).
// Tutte le chiamate viaggiano con il Bearer token via useApiClient().

export interface ColumnInfo {
  name: string
  dtype: string
}

export interface PreviewResult {
  columns: ColumnInfo[]
  rows: Record<string, any>[]
  row_count: number
  truncated: boolean
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

export interface Operation {
  type: string
  params: Record<string, any>
}

export function useApi() {
  const { apiFetch } = useApiClient()

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
    }): Promise<PreviewResult> {
      return await apiFetch<PreviewResult>('/tasks/preview', { method: 'POST', body })
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
    async engines(): Promise<{ id: string; label: string; available: boolean; description: string }[]> {
      return await apiFetch('/engines')
    },

    // info di deployment (fuso orario degli schedule, nome, versione)
    async appInfo(): Promise<{ name: string; version: string; timezone: string }> {
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
    async auditSessions(): Promise<ActiveSession[]> {
      return await apiFetch<ActiveSession[]>('/audit/sessions')
    },
    async auditAccessActivity(hours = 24): Promise<AccessActivity> {
      return await apiFetch<AccessActivity>(`/audit/access-activity?hours=${hours}`)
    },

    // grafo di lineage: senza center → grafo completo leggibile; con center →
    // sottografo a monte/valle entro `depth` salti
    async lineage(p?: {
      type?: 'flow' | 'datasource'
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

    async taskStatus(id: string): Promise<TaskStatus> {
      return await apiFetch<TaskStatus>(`/tasks/${id}`)
    },

    // download diretto del risultato (anche da un nodo intermedio) come csv/xlsx
    async exportData(body: {
      bucket: string
      input_key: string
      operations: Operation[]
      format: 'csv' | 'xlsx'
      limit?: number
      filename?: string
      engine?: string // motore che calcola lo snapshot; il file lo scrive Polars
    }): Promise<Blob> {
      return await apiFetch<Blob>('/tasks/export', { method: 'POST', body, responseType: 'blob' })
    },
  }
}

// ── Lineage cross-flow ───────────────────────────────────────────────────────
export type LineageNodeType = 'flow' | 'datasource' | 'connection' | 'db_sink' | 's3_sink'
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
export interface ActiveSession {
  user_id: number
  email: string
  full_name: string
  is_superuser: boolean
  last_seen_at: string | null
  last_seen_ip: string | null
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
  return e?.data?.detail ?? e?.data?.message ?? e?.message ?? 'Errore sconosciuto'
}
