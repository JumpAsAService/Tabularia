// Esecuzioni dei flussi (run history) via gateway. Il lancio richiede la
// capability RUN sul progetto del flusso; la cronologia basta VIEW.
import type { Page } from '~/composables/useApi'

export interface RunInfo {
  id: number
  kind?: string
  flow_id: number
  status: 'PENDING' | 'STARTED' | 'SUCCESS' | 'FAILURE' | string
  launched_by: number | null
  trigger_type?: string // "manual" | "schedule"
  output_key: string
  rows_written: number | null
  error: string | null
  error_detail?: string | null // traceback completo (dettaglio del fallimento)
  publish_name: string | null
  datasource_id: number | null
  destination: string | null // JSON: {db_type, host, database, table, mode}
  started_at: string | null
  finished_at: string | null
  // presenti solo nella ricerca globale /runs
  flow_name?: string | null
  source_name?: string | null
  launched_by_name?: string | null // nome di chi l'ha avviato (null se schedulato)
}

// un bucket del calendar plot: un giorno (key=YYYY-MM-DD) o un'ora (key='00'..'23')
export interface ActivityBucket {
  key: string
  total: number
  success: number
  failure: number
  scheduled: number
  manual: number
}
export interface RunActivity {
  granularity: 'day' | 'hour'
  from_key: string
  to_key: string
  buckets: ActivityBucket[]
}

export interface PublishSpec {
  name: string
  project_id: number
  description?: string
  overwrite?: boolean
}

export interface DestinationSpec {
  type: 'database' | 's3'
  connection_id: number
  // type="database"
  table?: string
  mode?: 'append' | 'replace'
  post_sql?: string
  // type="s3"
  bucket?: string
  key?: string
  format?: 'parquet' | 'csv'
  partition_by?: string[]
}

export function useRuns() {
  const { apiFetch } = useApiClient()

  return {
    launch: (
      flowId: number,
      body: {
        bucket: string
        input_key: string
        operations: any[]
        publish?: PublishSpec | null
        destination?: DestinationSpec | null
      },
    ) => apiFetch<RunInfo>(`/flows/${flowId}/runs`, { method: 'POST', body }),

    listByFlow: (flowId: number) => apiFetch<RunInfo[]>(`/flows/${flowId}/runs`),

    get: (id: number) => apiFetch<RunInfo>(`/runs/${id}`),

    // ricerca globale PAGINATA delle esecuzioni (nei progetti leggibili): filtro
    // per stato e ricerca testuale sul motivo, sull'intero dataset
    search: (params: { status?: string; q?: string; limit?: number; offset?: number }) => {
      const qs = new URLSearchParams()
      if (params.status) qs.set('status', params.status)
      if (params.q) qs.set('q', params.q)
      qs.set('limit', String(params.limit ?? 50))
      qs.set('offset', String(params.offset ?? 0))
      return apiFetch<Page<RunInfo>>(`/runs?${qs.toString()}`)
    },

    // attività per il calendar plot: conteggi per giorno (heatmap) o, con `day`,
    // per ora (drill-down). tz_offset allinea i bucket all'ora locale del client.
    activity: (params: { days?: number; day?: string } = {}) => {
      const qs = new URLSearchParams()
      if (params.days) qs.set('days', String(params.days))
      if (params.day) qs.set('day', params.day)
      qs.set('tz_offset', String(new Date().getTimezoneOffset()))
      return apiFetch<RunActivity>(`/runs/activity?${qs.toString()}`)
    },
  }
}
