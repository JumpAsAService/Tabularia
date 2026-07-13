// Flussi salvati (DAG dell'editor) dentro i progetti. Via gateway autenticato.

export interface FlowSummary {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  run_schedule: string | null // cron; null = non schedulato
  next_run_at: string | null
  updated_at: string | null
}

export interface FlowDetail extends FlowSummary {
  definition: string // JSON serializzato del canvas: { nodes, edges }
}

export function useFlows() {
  const { apiFetch } = useApiClient()

  return {
    // tutti i flussi nei progetti leggibili (pagina globale Flows)
    list: () => apiFetch<FlowSummary[]>('/flows'),

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

    // imposta/disabilita l'esecuzione schedulata (cron a 5 campi; '' = disabilita)
    setSchedule: (id: number, cron: string) =>
      apiFetch<FlowDetail>(`/flows/${id}/schedule`, { method: 'PUT', body: { cron } }),
  }
}
