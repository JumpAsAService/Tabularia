// Dashboards: a screen of live tiles on a grid, kept in a folder with its versions
// (docs/specs/dashboards.md). The rules are the gateway's (routes/dashboards.py): VIEW opens a
// dashboard, EDIT changes it; what a tile reads is checked with the reader's rights.

export interface DashboardDefinition {
  v: 1
  locale: string
  timezone: string
  refresh_seconds: number // 0: by hand only; otherwise at least 60
  parameters: Record<string, any>[]
  datasets: Record<string, any>[]
  tiles: Record<string, any>[]
}

export interface Dashboard {
  id: number
  name: string
  description: string
  project_id: number
  current_version: number
  datasets: number
  parameters: number
  tiles: number
  created_at: string
  updated_at: string
  editable: boolean
}

export interface DashboardDetail extends Dashboard {
  content: DashboardDefinition
}

/** A new dashboard: no data yet, refreshed by hand, in the language of whoever makes it. */
export function starterDashboard(locale: string, timezone: string): DashboardDefinition {
  return {
    v: 1,
    locale: ['en', 'it', 'de', 'es', 'fr'].includes(locale) ? locale : 'en',
    timezone: timezone || 'UTC',
    refresh_seconds: 0,
    parameters: [],
    datasets: [],
    tiles: [],
  }
}

export function useDashboards() {
  const { apiFetch } = useApiClient()

  return {
    listByProject: (projectId: number) => apiFetch<Dashboard[]>(`/projects/${projectId}/dashboards`),
    get: (id: number) => apiFetch<DashboardDetail>(`/dashboards/${id}`),
    /** the viewer has it on screen: in the audit, and in the count of who reads it */
    opened: (id: number) => apiFetch<void>(`/dashboards/${id}/views`, { method: 'POST' }),
    create: (projectId: number, body: { name: string; description?: string; content?: DashboardDefinition }) =>
      apiFetch<DashboardDetail>(`/projects/${projectId}/dashboards`, { method: 'POST', body }),
    update: (id: number, body: { name?: string; description?: string; project_id?: number; content?: DashboardDefinition; base_version?: number }) =>
      apiFetch<DashboardDetail>(`/dashboards/${id}`, { method: 'PATCH', body }),
    duplicate: (id: number) => apiFetch<DashboardDetail>(`/dashboards/${id}/duplicate`, { method: 'POST' }),
    remove: (id: number) => apiFetch<void>(`/dashboards/${id}`, { method: 'DELETE' }),
  }
}
