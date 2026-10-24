// Esecuzioni dei flussi (run history) via gateway. Il lancio richiede la
// capability RUN sul progetto del flusso; la cronologia basta VIEW.
import type { Page } from '~/composables/useApi'

// a che punto è un run non terminato (solo per quelli; vedi useRunProgress)
export interface RunProgress {
  // queued · running · waiting (il database non ha ancora mandato righe) ·
  // reading · files (SharePoint) · saving · checking (data contract)
  phase: string
  seconds: number // contati dal gateway
  rows?: number | null
  file?: number | null
  files?: number | null
  call?: number | null // API: call `call` of `calls`, `pages` so far
  calls?: number | null
  pages?: number | null
}

export interface RunInfo {
  id: number
  kind?: string
  flow_id: number | null // null for the run of a flow group
  flow_group_id?: number | null
  status: 'PENDING' | 'STARTED' | 'SUCCESS' | 'FAILURE' | string
  launched_by: number | null
  trigger_type?: string // "manual" | "schedule"
  output_key: string
  rows_written: number | null
  error: string | null
  error_detail?: string | null // traceback completo (dettaglio del fallimento)
  publish_name: string | null
  datasource_id: number | null
  // JSON: {db_type, host, database, table, mode} | s3 {bucket, key, written_key?} | sftp {host, folder, file_name, path}
  destination: string | null
  // JSON of a successful refresh that skipped values: {missing: [{params, pattern}],
  // missing_count, expansions} — read it with runNotes() (usePlaceholders)
  notes?: string | null
  // kind "script": JSON — environment, version, files, attempts, exit code, the end of
  // the log, the variables it was given (secret ones by name only); see ScriptRunDetail
  script?: string | null
  // the email sent: JSON — recipients, subject, attachments, outcome (a Pushmail cell's run too)
  email?: string | null
  parent_run_id?: number | null
  started_at: string | null
  finished_at: string | null
  progress?: RunProgress | null
  // presenti solo nella ricerca globale /runs
  flow_name?: string | null
  source_name?: string | null
  group_name?: string | null
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
  sort_keys?: string[] // ORDER BY della datasource pubblicata (vuoto = nessuno)
}

export interface DestinationSpec {
  type: 'database' | 's3' | 'sftp'
  connection_id: number
  // type="database"
  table?: string
  mode?: 'append' | 'replace'
  post_sql?: string
  // type="s3"
  bucket?: string
  key?: string
  format?: 'parquet' | 'csv' | 'xlsx' // xlsx: sftp only
  partition_by?: string[]
  // type="sftp": folder on the server and file-name template ({date}, {datetime}, {run_id}, {flow})
  folder?: string
  file_name?: string
  // the zone of the dates in the templates (sftp folder and file name, s3 key); empty = UTC
  timezone?: string
}

// Invio dell'output come allegato. I destinatari sono limitati dai domini
// ammessi sulla CONNESSIONE e il controllo è del gateway: quello che si scrive
// qui è una proposta, non un permesso.
export interface EmailSpec {
  connection_id: number
  to: string[]
  cc?: string[]
  subject: string
  body?: string
  body_is_html?: boolean
  attachment_name?: string
  attachment_format?: 'csv' | 'xlsx'
  stop_on_failure?: boolean
  timezone?: string // of the dates in the subject and the attachment name; empty = UTC
}

export interface MirrorSpec {
  connection_id: number
  bucket: string
  key: string
  timezone?: string
}

interface RunBody {
  bucket: string
  input_key: string
  operations: any[]
  publish?: PublishSpec | null
  destination?: DestinationSpec | null
  email?: EmailSpec | null
  // the S3 copy of a published datasource (the Output node's «copy» option)
  mirror?: MirrorSpec | null
}

export function useRuns() {
  const { apiFetch } = useApiClient()

  return {
    launch: (flowId: number, body: RunBody) =>
      apiFetch<RunInfo>(`/flows/${flowId}/runs`, { method: 'POST', body }),

    // prova a vuoto: stesso corpo, ma il DESTINATARIO lo impone il server
    // (l'utente collegato) e publish/destination vengono scartati. Mandarsi
    // l'allegato è l'unico modo di vedere davvero cosa riceverà chi di dovere.
    emailTest: (flowId: number, body: RunBody) =>
      apiFetch<RunInfo>(`/flows/${flowId}/email-test`, { method: 'POST', body }),

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
