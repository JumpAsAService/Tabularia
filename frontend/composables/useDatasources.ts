// Datasource nominate: il catalogo dei dataset riusabili come sorgenti nei flussi.
// kind="flow": pubblicate da un run. kind="database": snapshot di una tabella o
// query su una connessione esterna, aggiornabile con `refresh`.
import type { RunInfo } from '~/composables/useRuns'
import { pagedQuery, type Page } from '~/composables/useApi'
import type { ContractSummary } from '~/composables/useContracts'

export interface DatasourceInfo {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  bucket: string
  key: string
  rows: number | null
  columns: { name: string; dtype: string; description?: string }[]
  column_descriptions: Record<string, string> // {colonna: descrizione}, curate a mano
  // quanto è documentata; la regola la decide il SERVER (services/documented.py)
  described_columns: number
  total_columns: number
  ai_ready: boolean // descrizione della tabella + tutti i campi descritti
  sort_keys: string[] // colonne di ORDER BY (ordinano parquet + copia materializzata)
  kind: string
  flow_id: number | null
  connection_id: number | null
  source_type: 'table' | 'sql' | 'sharepoint' | 'sftp' | 'api' | null
  source_ref: string | null
  refreshed_at: string | null
  refreshing?: boolean // un ingest è in corso ORA (lo dice il server)
  refresh_schedule: string | null // espressione cron; null = non schedulato
  next_refresh_at: string | null
  updated_at: string | null
  // il data contract, se ce n'è uno acceso: stato per l'icona (null = nessuno)
  contract?: ContractSummary | null
}

export interface DbDatasourceDraft {
  name: string
  description?: string
  connection_id: number
  source_type: 'table' | 'sql'
  source_ref: string
  sort_keys?: string[] // ORDER BY dell'import (vuoto = nessun ordine)
}

// files of a folder on an SFTP server: folder + glob, every match stacked
export interface SftpDatasourceDraft {
  name: string
  description?: string
  connection_id: number
  folder: string
  pattern: string
  format: 'auto' | 'csv' | 'parquet' | 'excel' | 'ndjson'
  sheet: string
  csv: { separator: string; has_header: boolean; encoding: 'utf8' | 'utf8-lossy' | 'windows-1252' }
}

// the call of an «API call» datasource (see gateway ApiCallSpec): placeholders such
// as {today-7d} in path, query, headers and body; list variables make one call per value
export interface ApiPair { name: string; value: string }
export interface ApiCallDraft {
  method: 'GET' | 'POST'
  path: string
  query: ApiPair[]
  headers: ApiPair[]
  body: string
  variables: { name: string; values: string[] }[]
  timezone: string
  last_refresh_initial: string
  response: {
    format: 'json' | 'ndjson' | 'csv'
    records_path: string
    jmespath: string
    keyed: boolean
    explode: string
    csv_separator: string
  }
  pagination: {
    type: 'none' | 'page' | 'offset' | 'cursor' | 'next_link'
    param: string
    start: number | null
    limit_param: string
    limit: number
    cursor_path: string
    next_path: string
  }
  limits: { max_pages: number; max_calls: number; max_rows: number; min_interval_seconds: number }
  mode: 'snapshot' | 'append'
  keys: string[]
}
export interface ApiDatasourceDraft extends ApiCallDraft {
  name: string
  description?: string
  connection_id: number
}

export function emptyApiCall(): ApiCallDraft {
  return {
    method: 'GET', path: '', query: [], headers: [], body: '', variables: [], timezone: 'UTC', last_refresh_initial: '',
    response: { format: 'json', records_path: '', jmespath: '', keyed: false, explode: '', csv_separator: ',' },
    pagination: { type: 'none', param: '', start: null, limit_param: '', limit: 100, cursor_path: '', next_path: '' },
    limits: { max_pages: 0, max_calls: 0, max_rows: 0, min_interval_seconds: 0 },
    mode: 'snapshot', keys: [],
  }
}

// numbers typed in the form may be empty strings: the server wants numbers or null
export function cleanApiCall(c: ApiCallDraft): ApiCallDraft {
  const num = (v: unknown, empty: number | null) => (v === '' || v === null || v === undefined || Number.isNaN(Number(v)) ? empty : Number(v))
  return {
    ...c,
    path: c.path.trim(),
    timezone: c.timezone.trim() || 'UTC',
    last_refresh_initial: c.last_refresh_initial.trim(),
    body: c.method === 'POST' ? c.body : '',
    query: c.query.filter((p) => p.name),
    headers: c.headers.filter((p) => p.name),
    variables: c.variables.filter((v) => v.name),
    response: { ...c.response, records_path: c.response.records_path.trim(), jmespath: c.response.jmespath.trim(), explode: c.response.explode.trim() },
    pagination: { ...c.pagination, start: num(c.pagination.start, null), limit: num(c.pagination.limit, 100) ?? 100 },
    limits: {
      max_pages: num(c.limits.max_pages, 0) ?? 0,
      max_calls: num(c.limits.max_calls, 0) ?? 0,
      max_rows: num(c.limits.max_rows, 0) ?? 0,
      min_interval_seconds: num(c.limits.min_interval_seconds, 0) ?? 0,
    },
    keys: c.mode === 'append' ? c.keys : [],
  }
}

export interface SharePointDatasourceDraft {
  name: string
  description?: string
  connection_id: number
  path: string // percorso nella raccolta, anche con glob: Budget/2026/*.xlsx
  sheet: string
}

export function useDatasources() {
  const { apiFetch } = useApiClient()

  return {
    // tutte quelle nei progetti visibili (per il picker delle sorgenti)
    list: () => apiFetch<DatasourceInfo[]>('/datasources'),

    // pagina globale: ricerca server-side (sull'intero dataset) + paginazione
    listPaged: (p: { q?: string; limit: number; offset: number }) =>
      apiFetch<Page<DatasourceInfo>>(`/datasources/search?${pagedQuery(p)}`),

    listByProject: (projectId: number) =>
      apiFetch<DatasourceInfo[]>(`/projects/${projectId}/datasources`),

    // datasource da database: crea la voce e lancia il primo ingest
    // datasource da Excel su SharePoint: più file del glob = una tabella sola
    createSharePoint: (projectId: number, body: SharePointDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/sharepoint`, { method: 'POST', body }),
    // datasource from an HTTP API: the call is made at each refresh
    createApi: (projectId: number, body: ApiDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/api`, { method: 'POST', body }),
    // datasource from an SFTP folder: the matching files stacked into one table
    createSftp: (projectId: number, body: SftpDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/sftp`, { method: 'POST', body }),
    createDb: (projectId: number, body: DbDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/database`, {
        method: 'POST',
        body,
      }),

    // ri-esegue la sorgente e sostituisce lo snapshot (torna il run da pollare)
    refresh: (id: number) => apiFetch<RunInfo>(`/datasources/${id}/refresh`, { method: 'POST' }),

    // imposta/disabilita il refresh schedulato (cron a 5 campi; '' = disabilita)
    setSchedule: (id: number, cron: string) =>
      apiFetch<DatasourceInfo>(`/datasources/${id}/schedule`, { method: 'PUT', body: { cron } }),

    // cronologia degli ingest (il GET riconcilia gli stati)
    listRuns: (id: number) => apiFetch<RunInfo[]>(`/datasources/${id}/runs`),

    update: (
      id: number,
      body: Partial<{
        name: string
        description: string
        project_id: number
        column_descriptions: Record<string, string>
        sort_keys: string[] // database datasources only; applies from the next refresh
      }>,
    ) => apiFetch<DatasourceInfo>(`/datasources/${id}`, { method: 'PATCH', body }),

    remove: (id: number) => apiFetch<void>(`/datasources/${id}`, { method: 'DELETE' }),

    // definition and data: the engine copies the data file on the storage
    duplicate: (id: number) => apiFetch<DatasourceInfo>(`/datasources/${id}/duplicate`, { method: 'POST' }),
  }
}
