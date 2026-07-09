// Connessioni a database esterni. Tutte le operazioni richiedono la capability
// CONNECT sulla cartella della connessione; la password non esce mai dalle API.

export interface ConnectionInfo {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  db_type: string
  host: string
  port: number | null
  username: string
  database: string
  db_schema: string
  has_password: boolean
  updated_at: string | null
}

export interface ConnectionDraft {
  name: string
  description?: string
  db_type: string
  host: string
  port?: number | null
  username?: string
  password?: string
  database?: string
  db_schema?: string
}

export const DB_TYPES = [
  { value: 'postgresql', label: 'PostgreSQL' },
  { value: 'mysql', label: 'MySQL' },
  { value: 'mariadb', label: 'MariaDB' },
  { value: 'clickhouse', label: 'ClickHouse' },
  { value: 'trino', label: 'Trino' },
]

export function useConnections() {
  const { apiFetch } = useApiClient()

  return {
    // tutte quelle usabili dall'utente (per il picker delle sorgenti DB)
    list: () => apiFetch<ConnectionInfo[]>('/connections'),

    listByProject: (projectId: number) =>
      apiFetch<ConnectionInfo[]>(`/projects/${projectId}/connections`),

    create: (projectId: number, body: ConnectionDraft) =>
      apiFetch<ConnectionInfo>(`/projects/${projectId}/connections`, { method: 'POST', body }),

    update: (id: number, body: Partial<ConnectionDraft> & { project_id?: number }) =>
      apiFetch<ConnectionInfo>(`/connections/${id}`, { method: 'PATCH', body }),

    remove: (id: number) => apiFetch<void>(`/connections/${id}`, { method: 'DELETE' }),

    // test di una connessione GIÀ salvata
    test: (id: number) => apiFetch<{ ok: boolean }>(`/connections/${id}/test`, { method: 'POST' }),

    // test del form PRIMA di salvare
    testDraft: (projectId: number, body: ConnectionDraft) =>
      apiFetch<{ ok: boolean }>(`/projects/${projectId}/connections/test`, { method: 'POST', body }),

    tables: (id: number) => apiFetch<{ tables: string[] }>(`/connections/${id}/tables`),
  }
}
