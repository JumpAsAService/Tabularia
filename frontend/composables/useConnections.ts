// Connessioni a database esterni. Tutte le operazioni richiedono la capability
// CONNECT sulla cartella della connessione; la password non esce mai dalle API.
import { pagedQuery, type Page } from '~/composables/useApi'

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
  // opzioni del tipo, JSON (solo SMTP: from_address, from_name, tls,
  // allowed_domains). Non contiene segreti, per questo torna al client.
  extra: string
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
  extra?: string
}

export const DB_TYPES = [
  { value: 'postgresql', label: 'PostgreSQL' },
  { value: 'mysql', label: 'MySQL' },
  { value: 'mariadb', label: 'MariaDB' },
  { value: 'clickhouse', label: 'ClickHouse' },
  { value: 'trino', label: 'Trino' },
  // object storage: host=endpoint, username=access key, database=bucket, schema=region
  { value: 's3', label: 'S3 / object storage' },
  // posta: le tre impostazioni senza colonna naturale (mittente, TLS, domini
  // ammessi) vivono in `extra`
  { value: 'smtp', label: 'SMTP / email' },
  // file Excel su SharePoint via Microsoft Graph: host=URL del sito,
  // username=client id dell'app Entra ID, password=secret, database=tenant id,
  // extra.library=raccolta documenti
  { value: 'sharepoint', label: 'SharePoint (Excel)' },
]

export function useConnections() {
  const { apiFetch } = useApiClient()

  return {
    // tutte quelle usabili dall'utente (per il picker delle sorgenti DB)
    list: () => apiFetch<ConnectionInfo[]>('/connections'),

    // pagina globale: ricerca server-side (sull'intero dataset) + paginazione
    listPaged: (p: { q?: string; limit: number; offset: number }) =>
      apiFetch<Page<ConnectionInfo>>(`/connections/search?${pagedQuery(p)}`),

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

    // i file Excel che un percorso/glob prende su una connessione SharePoint
    sharepointFiles: (id: number, path: string) =>
      apiFetch<{ files: { path: string; size: number; modified_at: string }[]; total: number }>(
        `/connections/${id}/sharepoint/files`, { method: 'POST', body: { path } }),
    tables: (id: number) => apiFetch<{ tables: string[] }>(`/connections/${id}/tables`),
  }
}
