// Flussi salvati (DAG dell'editor) dentro i progetti. Via gateway autenticato.
import { pagedQuery, type Page } from '~/composables/useApi'

export interface FlowSummary {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  owner_name: string | null // nome di chi ha creato il flusso
  run_schedule: string | null // cron; null = non schedulato
  next_run_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface FlowDetail extends FlowSummary {
  definition: string // JSON serializzato del canvas: { nodes, edges }
}

export interface FlowStats {
  run_count: number
  success_count: number
  failure_count: number
  last_run_at: string | null
  avg_duration_seconds: number | null
}

export interface FlowVersionInfo {
  version: number
  note: string
  created_at: string | null
  created_by: number | null
  is_current: boolean
}

export function useFlows() {
  const { apiFetch } = useApiClient()

  return {
    // tutti i flussi nei progetti leggibili (per i picker; non paginato)
    list: () => apiFetch<FlowSummary[]>('/flows'),

    // pagina globale: ricerca server-side (sull'intero dataset) + paginazione
    listPaged: (p: { q?: string; limit: number; offset: number }) =>
      apiFetch<Page<FlowSummary>>(`/flows/search?${pagedQuery(p)}`),

    listByProject: (projectId: number) =>
      apiFetch<FlowSummary[]>(`/projects/${projectId}/flows`),

    get: (id: number) => apiFetch<FlowDetail>(`/flows/${id}`),

    create: (projectId: number, body: { name: string; description?: string; definition: string }) =>
      apiFetch<FlowDetail>(`/projects/${projectId}/flows`, { method: 'POST', body }),

    update: (
      id: number,
      body: Partial<{ name: string; description: string; definition: string; project_id: number }>,
    ) => apiFetch<FlowDetail>(`/flows/${id}`, { method: 'PATCH', body }),

    remove: (id: number) => apiFetch<void>(`/flows/${id}`, { method: 'DELETE' }),

    // statistiche d'esecuzione (n° run, successi/falliti, ultima, tempo medio)
    stats: (id: number) => apiFetch<FlowStats>(`/flows/${id}/stats`),

    // storico versioni della definizione + promozione di una versione vecchia
    versions: (id: number) => apiFetch<FlowVersionInfo[]>(`/flows/${id}/versions`),
    promote: (id: number, version: number) =>
      apiFetch<FlowDetail>(`/flows/${id}/versions/${version}/promote`, { method: 'POST' }),

    // imposta/disabilita l'esecuzione schedulata (cron a 5 campi; '' = disabilita)
    setSchedule: (id: number, cron: string) =>
      apiFetch<FlowDetail>(`/flows/${id}/schedule`, { method: 'PUT', body: { cron } }),

    // esegue subito l'orchestrazione (refresh → output → runflow) in background
    runNow: (id: number) =>
      apiFetch<{ status: string; flow_id: number; run_id: number }>(`/flows/${id}/run-now`, {
        method: 'POST',
      }),
  }
}
