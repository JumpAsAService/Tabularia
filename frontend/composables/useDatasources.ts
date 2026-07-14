// Datasource nominate: il catalogo dei dataset riusabili come sorgenti nei flussi.
// kind="flow": pubblicate da un run. kind="database": snapshot di una tabella o
// query su una connessione esterna, aggiornabile con `refresh`.
import type { RunInfo } from '~/composables/useRuns'
import { pagedQuery, type Page } from '~/composables/useApi'

export interface DatasourceInfo {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  bucket: string
  key: string
  rows: number | null
  columns: { name: string; dtype: string }[]
  kind: string
  flow_id: number | null
  connection_id: number | null
  source_type: 'table' | 'sql' | null
  source_ref: string | null
  refreshed_at: string | null
  refresh_schedule: string | null // espressione cron; null = non schedulato
  next_refresh_at: string | null
  updated_at: string | null
}

export interface DbDatasourceDraft {
  name: string
  description?: string
  connection_id: number
  source_type: 'table' | 'sql'
  source_ref: string
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
      body: Partial<{ name: string; description: string; project_id: number }>,
    ) => apiFetch<DatasourceInfo>(`/datasources/${id}`, { method: 'PATCH', body }),

    remove: (id: number) => apiFetch<void>(`/datasources/${id}`, { method: 'DELETE' }),
  }
}
