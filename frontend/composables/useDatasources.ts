// Named datasources: the catalog of the datasets reusable as sources in the flows.
// kind="flow": published by a run. kind="database": snapshot of a table or a query on an
// external connection, updatable with `refresh`.
import type { RunInfo } from '~/composables/useRuns'
import { pagedQuery, type Page } from '~/composables/useApi'
import type { ContractSummary } from '~/composables/useContracts'
import { cleanVariables, type FileTemplatesDraft, type TemplateVariable } from '~/composables/usePlaceholders'

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
  column_descriptions: Record<string, string> // {column: description}, curated by hand
  // how documented it is; the SERVER decides the rule (services/documented.py)
  described_columns: number
  total_columns: number
  ai_ready: boolean // the table's description + all the fields described
  sort_keys: string[] // ORDER BY columns (they sort parquet + materialized copy)
  kind: string
  flow_id: number | null
  connection_id: number | null
  source_type: 'table' | 'sql' | 'sharepoint' | 'sftp' | 's3' | 'api' | 'gsheets' | null
  source_ref: string | null
  refreshed_at: string | null
  refreshing?: boolean // an ingest is running NOW (the server says it)
  refresh_schedule: string | null // cron expression; null = not scheduled
  next_refresh_at: string | null
  updated_at: string | null
  // the data contract, when one is on: state for the icon (null = none)
  contract?: ContractSummary | null
}

export interface DbDatasourceDraft {
  name: string
  description?: string
  connection_id: number
  source_type: 'table' | 'sql'
  source_ref: string
  sort_keys?: string[] // the import's ORDER BY (empty = no order)
}

// files of a folder on an SFTP server: folder + glob, every match stacked; folder and
// glob may hold placeholders and variables (FileTemplatesDraft)
export interface SftpDatasourceDraft extends Partial<FileTemplatesDraft> {
  name: string
  description?: string
  connection_id: number
  folder: string
  pattern: string
  format: 'auto' | 'csv' | 'parquet' | 'excel' | 'ndjson'
  sheet: string
  csv: { separator: string; has_header: boolean; encoding: 'utf8' | 'utf8-lossy' | 'windows-1252' }
}

// the objects of a folder of a bucket («S3 folder»), through an S3 connection: the
// bucket (empty = the connection's default), folder + glob, the SFTP reading options
export interface S3DatasourceDraft extends Partial<FileTemplatesDraft> {
  name: string
  description?: string
  connection_id: number
  bucket: string
  folder: string
  pattern: string
  format: 'auto' | 'csv' | 'parquet' | 'excel' | 'ndjson'
  sheet: string
  csv: { separator: string; has_header: boolean; encoding: 'utf8' | 'utf8-lossy' | 'windows-1252' }
}

// the call of an «API call» datasource (see gateway ApiCallSpec): placeholders such
// as {today-7d} in path, query, headers and body; variables (lists and ranges) make
// one call per value
export interface ApiPair { name: string; value: string }
export interface ApiCallDraft {
  method: 'GET' | 'POST'
  path: string
  query: ApiPair[]
  headers: ApiPair[]
  body: string
  variables: TemplateVariable[]
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
    variables: cleanVariables(c.variables),
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

export interface SharePointDatasourceDraft extends Partial<FileTemplatesDraft> {
  name: string
  description?: string
  connection_id: number
  path: string // path in the library, globs too: Budget/2026/*.xlsx
  format: SharePointFormat // auto = Excel and CSV, by each file's extension
  sheet: string // Excel: empty = the first sheet
  csv: { separator: string; has_header: boolean; encoding: string }
}

export type SharePointFormat = 'auto' | 'excel' | 'csv'

// one tab of a Google Sheets spreadsheet (and an optional A1 range), read at every refresh
export interface GsheetsDatasourceDraft {
  name: string
  description?: string
  connection_id: number
  spreadsheet_id: string
  spreadsheet_name: string // shown only: the id finds it
  sheet_id: number | null // the tab's gid: a renamed tab is still found
  sheet: string
  range: string // A1:F500, B3:H, A:F; empty = the whole tab
}

export function useDatasources() {
  const { apiFetch } = useApiClient()

  return {
    // all those in the visible projects (for the sources' picker)
    list: () => apiFetch<DatasourceInfo[]>('/datasources'),

    // global page: server-side search (over the whole dataset) + pagination
    listPaged: (p: { q?: string; limit: number; offset: number }) =>
      apiFetch<Page<DatasourceInfo>>(`/datasources/search?${pagedQuery(p)}`),

    listByProject: (projectId: number) =>
      apiFetch<DatasourceInfo[]>(`/projects/${projectId}/datasources`),

    // datasource from a database: creates the item and launches the first ingest
    // datasource from Excel on SharePoint: several files of the glob = one table
    createSharePoint: (projectId: number, body: SharePointDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/sharepoint`, { method: 'POST', body }),
    // datasource from an HTTP API: the call is made at each refresh
    createApi: (projectId: number, body: ApiDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/api`, { method: 'POST', body }),
    // datasource from a tab of a Google Sheets spreadsheet
    createGsheets: (projectId: number, body: GsheetsDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/gsheets`, { method: 'POST', body }),
    // datasource from an SFTP folder: the matching files stacked into one table
    createSftp: (projectId: number, body: SftpDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/sftp`, { method: 'POST', body }),
    // datasource from a folder of a bucket: the matching objects stacked into one table
    createS3: (projectId: number, body: S3DatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/s3`, { method: 'POST', body }),
    createDb: (projectId: number, body: DbDatasourceDraft) =>
      apiFetch<DatasourceInfo>(`/projects/${projectId}/datasources/database`, {
        method: 'POST',
        body,
      }),

    // runs the source again and replaces the snapshot (returns the run to poll)
    refresh: (id: number) => apiFetch<RunInfo>(`/datasources/${id}/refresh`, { method: 'POST' }),

    // sets/disables the scheduled refresh (5-field cron; '' = disable)
    setSchedule: (id: number, cron: string) =>
      apiFetch<DatasourceInfo>(`/datasources/${id}/schedule`, { method: 'PUT', body: { cron } }),

    // ingest history (the GET reconciles the states)
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
