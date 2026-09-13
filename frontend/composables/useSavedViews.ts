// Viste salvate: una configurazione del Viewer (datasource + motore + filtri +
// campi calcolati + pivot) messa in una cartella, accanto a flussi e datasource.
//
// La vista SEGUE il dato: contiene un riferimento alla datasource e nessuna
// riga, quindi riaprirla rilegge lo snapshot corrente. È condivisa, non
// personale: la vede chiunque abbia VIEW sulla cartella, la modifica chi ha EDIT.
//
// `spec` è JSON opaco per il gateway — la forma la possiede il Viewer, qui sotto.

export interface SavedViewSpec {
  engine: string
  filters: { column: string; operator: string; value: string; value2: string }[]
  computedFields: { name: string; expr: string }[]
  pivotOn: boolean
  pivot: { index: string[]; on: string[]; values: string; func: string }
  outline: boolean
}

export interface SavedView {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  datasource_id: number
  datasource_name: string | null
  spec: string
  created_at: string | null
  updated_at: string | null
}

export function useSavedViews() {
  const { apiFetch } = useApiClient()

  return {
    list: () => apiFetch<SavedView[]>('/saved-views'),

    listByProject: (projectId: number) =>
      apiFetch<SavedView[]>(`/projects/${projectId}/saved-views`),

    get: (id: number) => apiFetch<SavedView>(`/saved-views/${id}`),

    create: (projectId: number, body: { name: string; description?: string; datasource_id: number; spec: string }) =>
      apiFetch<SavedView>(`/projects/${projectId}/saved-views`, { method: 'POST', body }),

    update: (id: number, body: { name?: string; description?: string; spec?: string; project_id?: number }) =>
      apiFetch<SavedView>(`/saved-views/${id}`, { method: 'PATCH', body }),

    remove: (id: number) => apiFetch<void>(`/saved-views/${id}`, { method: 'DELETE' }),
  }
}
