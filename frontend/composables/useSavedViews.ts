// Saved views: a Viewer configuration (datasource + engine + filters + computed fields +
// pivot) put in a folder, next to flows and datasources.
//
// The view FOLLOWS the data: it holds a reference to the datasource and no rows, so
// reopening it reads the current snapshot again. It is shared, not personal: anyone with
// VIEW on the folder sees it, whoever has EDIT changes it.
//
// `spec` is opaque JSON for the gateway — the Viewer owns its shape, below.

export interface SavedViewSpec {
  engine: string
  filters: { column: string; operator: string; value: string; value2: string }[]
  computedFields: { name: string; expr: string }[]
  pivotOn: boolean
  pivot: { index: string[]; on: string[]; values: string; func: string }
  outline: boolean
  // the tab shown and the chart's settings (views saved before have neither)
  tab?: 'table' | 'chart'
  chart?: ChartSettings | null
}

export interface ChartSettings {
  type: string
  x: string
  by: string
  func: string
  y: string
  xFunc: string
  xNum: string
  yFunc: string
  yNum: string
  topN: number
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

    duplicate: (id: number) => apiFetch<SavedView>(`/saved-views/${id}/duplicate`, { method: 'POST' }),
  }
}
