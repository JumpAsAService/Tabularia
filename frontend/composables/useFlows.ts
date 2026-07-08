// Flussi salvati (DAG dell'editor) dentro i progetti. Via gateway autenticato.

export interface FlowSummary {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  updated_at: string | null
}

export interface FlowDetail extends FlowSummary {
  definition: string // JSON serializzato del canvas: { nodes, edges }
}

export function useFlows() {
  const { apiFetch } = useApiClient()

  return {
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
  }
}
