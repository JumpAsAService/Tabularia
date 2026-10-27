// SQL scripts: SQL kept as an object of a folder, with its versions, run on the catalog
// (docs/specs/sql-scripts.md). The rules are the gateway's (routes/sql_scripts.py).
import type { PreviewResult } from '~/composables/useApi'

export interface SqlScriptContent {
  sql: string
  /** null = the installation's default engine */
  engine: string | null
  /** null = the catalog (connections come later) */
  connection_id: number | null
}

export interface SqlScript {
  id: number
  name: string
  description: string
  project_id: number
  current_version: number
  engine: string | null
  created_at: string
  updated_at: string
  /** EDIT on its folder: saving, moving, deleting */
  editable: boolean
}

export interface SqlScriptDetail extends SqlScript {
  content: SqlScriptContent
}

export interface SqlScriptVersion {
  version: number
  /** a code the interface translates: created, edited, restored from vN */
  note: string
  created_at: string
  created_by: string | null
  current: boolean
}

/** A datasource the person may query: the editor's list and its completion. */
export interface SqlCatalogItem {
  id: number
  name: string
  /** the folder's own name: how the SQL qualifies a name two folders share */
  folder: string
  path: string
  rows: number | null
  columns: { name: string; type: string }[]
  has_data: boolean
}

export interface SqlRunResult extends PreviewResult {
  engine: string
  elapsed_ms: number
  datasources: { id: number; name: string }[]
}

export interface SqlScriptUpdate {
  name?: string
  description?: string
  project_id?: number
  content?: Partial<SqlScriptContent>
}

export function useSqlScripts() {
  const { apiFetch } = useApiClient()
  return {
    list: () => apiFetch<SqlScript[]>('/sql-scripts'),
    listByProject: (projectId: number) => apiFetch<SqlScript[]>(`/projects/${projectId}/sql-scripts`),
    get: (id: number) => apiFetch<SqlScriptDetail>(`/sql-scripts/${id}`),
    create: (projectId: number, body: { name: string; description?: string; content?: Partial<SqlScriptContent> }) =>
      apiFetch<SqlScriptDetail>(`/projects/${projectId}/sql-scripts`, { method: 'POST', body }),
    update: (id: number, body: SqlScriptUpdate) => apiFetch<SqlScriptDetail>(`/sql-scripts/${id}`, { method: 'PATCH', body }),
    duplicate: (id: number) => apiFetch<SqlScriptDetail>(`/sql-scripts/${id}/duplicate`, { method: 'POST' }),
    remove: (id: number) => apiFetch<void>(`/sql-scripts/${id}`, { method: 'DELETE' }),
    versions: (id: number) => apiFetch<SqlScriptVersion[]>(`/sql-scripts/${id}/versions`),
    version: (id: number, version: number) => apiFetch<SqlScriptContent>(`/sql-scripts/${id}/versions/${version}`),
    restore: (id: number, version: number) =>
      apiFetch<SqlScriptDetail>(`/sql-scripts/${id}/versions/${version}/restore`, { method: 'POST' }),
    catalog: () => apiFetch<SqlCatalogItem[]>('/sql-scripts/catalog'),
    run: (body: { sql: string; engine?: string | null; limit?: number; script_id?: number | null }, signal?: AbortSignal) =>
      apiFetch<SqlRunResult>('/sql-scripts/run', { method: 'POST', body, signal }),
  }
}

/** How a datasource is written in the SQL: its name in double quotes, with the folder's
 * in front when another readable datasource has the same name. */
export function sqlName(item: SqlCatalogItem, all: SqlCatalogItem[]): string {
  const q = (s: string) => `"${s.replace(/"/g, '""')}"`
  const homonyms = all.filter((d) => d.name.toLowerCase() === item.name.toLowerCase()).length > 1
  return homonyms ? `${q(item.folder)}.${q(item.name)}` : q(item.name)
}
