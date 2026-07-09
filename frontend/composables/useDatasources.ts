// Datasource nominate: il catalogo dei dataset riusabili come sorgenti nei flussi.

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
  updated_at: string | null
}

export function useDatasources() {
  const { apiFetch } = useApiClient()

  return {
    // tutte quelle nei progetti visibili (per il picker delle sorgenti)
    list: () => apiFetch<DatasourceInfo[]>('/datasources'),

    listByProject: (projectId: number) =>
      apiFetch<DatasourceInfo[]>(`/projects/${projectId}/datasources`),

    update: (
      id: number,
      body: Partial<{ name: string; description: string; project_id: number }>,
    ) => apiFetch<DatasourceInfo>(`/datasources/${id}`, { method: 'PATCH', body }),

    remove: (id: number) => apiFetch<void>(`/datasources/${id}`, { method: 'DELETE' }),
  }
}
