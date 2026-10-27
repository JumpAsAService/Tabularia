// SQL scripts: SQL kept as an object of a folder, with its versions, run on the catalog or
// on a database connection (docs/specs/sql-scripts.md). The rules are the gateway's
// (routes/sql_scripts.py).
import type { PreviewResult } from '~/composables/useApi'

export interface SqlScriptContent {
  sql: string
  /** null = the installation's default engine */
  engine: string | null
  /** null = the catalog */
  connection_id: number | null
}

export interface SqlScript {
  id: number
  name: string
  description: string
  project_id: number
  current_version: number
  engine: string | null
  /** the current version's connection; null = the catalog */
  connection: SqlScriptConnection | null
  created_at: string
  updated_at: string
  /** EDIT on its folder: saving, moving, deleting */
  editable: boolean
}

export interface SqlScriptConnection {
  id: number
  /** empty when the connection no longer exists */
  name: string
  db_type: string
  /** CONNECT on its folder: this person can run the script */
  usable: boolean
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
  /** null on a connection */
  engine: string | null
  elapsed_ms: number
  datasources: { id: number; name: string }[]
  connection: { id: number; name: string; db_type: string } | null
}

/** A table the editor completes and the side list shows, with what to write for it. */
export interface SqlTable {
  name: string
  insert: string
  detail: string
  columns: { name: string; insert: string; type: string }[]
}

/** The databases a SQL script runs on (connections of other kinds hold no tables). */
export const QUERYABLE_DB_TYPES = ['postgresql', 'mysql', 'mariadb', 'clickhouse', 'trino', 'oracle', 'sqlserver']

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
    run: (body: { sql: string; engine?: string | null; limit?: number; script_id?: number | null; connection_id?: number | null },
      signal?: AbortSignal) =>
      apiFetch<SqlRunResult>('/sql-scripts/run', { method: 'POST', body, signal }),
    // the whole result as a file, written by a worker: followed and saved with useApi's followExport
    export: (body: { sql: string; format: 'csv' | 'xlsx'; engine?: string | null; script_id?: number | null; connection_id?: number | null }) =>
      apiFetch<{ task_id: string; filename: string }>('/sql-scripts/export', { method: 'POST', body }),
  }
}

/** How a datasource is written in the SQL: its name in double quotes, with the folder's
 * in front when another readable datasource has the same name. */
export function sqlName(item: SqlCatalogItem, all: SqlCatalogItem[]): string {
  const q = (s: string) => `"${s.replace(/"/g, '""')}"`
  const homonyms = all.filter((d) => d.name.toLowerCase() === item.name.toLowerCase()).length > 1
  return homonyms ? `${q(item.folder)}.${q(item.name)}` : q(item.name)
}

// How each database quotes a name, and which names need no quotes: Postgres and Trino
// fold the bare ones to lowercase, Oracle to uppercase.
const DIALECT_QUOTES: Record<string, { open: string; close: string; bare: RegExp }> = {
  postgresql: { open: '"', close: '"', bare: /^[a-z_][a-z0-9_]*$/ },
  trino: { open: '"', close: '"', bare: /^[a-z_][a-z0-9_]*$/ },
  oracle: { open: '"', close: '"', bare: /^[A-Z][A-Z0-9_$#]*$/ },
  mysql: { open: '`', close: '`', bare: /^[A-Za-z_][A-Za-z0-9_]*$/ },
  mariadb: { open: '`', close: '`', bare: /^[A-Za-z_][A-Za-z0-9_]*$/ },
  clickhouse: { open: '`', close: '`', bare: /^[A-Za-z_][A-Za-z0-9_]*$/ },
  sqlserver: { open: '[', close: ']', bare: /^[A-Za-z_][A-Za-z0-9_]*$/ },
}

/** A column's name as a SQL of `dbType` writes it (null = the catalog: the engines' SQL). */
export function sqlIdent(name: string, dbType: string | null): string {
  const d = (dbType && DIALECT_QUOTES[dbType]) || { open: '"', close: '"', bare: /^[A-Za-z_][A-Za-z0-9_]*$/ }
  return d.bare.test(name) ? name : `${d.open}${name.replaceAll(d.close, d.close + d.close)}${d.close}`
}

/** A table as the connection lists it, written for its SQL. Oracle and SQL Server list
 * their names quoted where needed already; the others, `schema.table` bare. */
export function sqlTableRef(listed: string, dbType: string): string {
  if (dbType === 'oracle' || dbType === 'sqlserver') return listed
  return listed.split('.').map((part) => sqlIdent(part, dbType)).join('.')
}