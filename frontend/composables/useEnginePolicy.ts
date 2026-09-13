// Motori che l'installazione consente di SCEGLIERE. Li governa un
// amministratore dal pannello Admin; tutti gli altri ne vedono solo l'effetto —
// un motore non consentito sparisce dalle scelte possibili del selettore.
//
// `flows_using` è la parte che conta: disabilitare un motore NON ferma i flussi
// che lo usano già (un interruttore non deve poter fermare un DAG schedulato),
// quindi il pannello mostra quanti restano fuori standard. Senza quel numero la
// standardizzazione sarebbe un interruttore senza conseguenze visibili.

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
