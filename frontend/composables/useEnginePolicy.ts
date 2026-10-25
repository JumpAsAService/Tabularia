// The engines the installation allows to CHOOSE. An administrator governs them from the
// Admin panel; everyone else sees only their effect — an engine not allowed disappears
// from the selector's possible choices.
//
// `flows_using` is the part that counts: disabling an engine does NOT stop the flows that
// use it already (a switch must not be able to stop a scheduled DAG), so the panel shows
// how many stay off standard. Without that number the standardization would be a switch
// without visible consequences.

export interface EnginePolicy {
  engine_id: string
  allowed: boolean
  flows_using: number
}

export function useEnginePolicy() {
  const { apiFetch } = useApiClient()

  return {
    list: () => apiFetch<EnginePolicy[]>('/engine-policy'),

    set: (engineId: string, allowed: boolean) =>
      apiFetch<EnginePolicy>(`/engine-policy/${engineId}`, { method: 'PUT', body: { allowed } }),
  }
}
