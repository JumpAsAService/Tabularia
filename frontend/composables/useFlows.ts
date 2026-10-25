// Saved flows (the editor's DAGs) inside the projects. Through the authenticated gateway.
import { pagedQuery, type Page } from '~/composables/useApi'

export interface FlowSummary {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  owner_name: string | null // the name of whoever created the flow
  engine: string // DEVELOPMENT engine (editor, manual runs)
  production_engine: string | null // the SCHEDULED runs' engine; null = as engine
  run_schedule: string | null
  notify_emails?: string | null
  notify_connection_id?: number | null // cron; null = not scheduled
  next_run_at: string | null
  created_at: string | null
  updated_at: string | null
  // the last run's state: SUCCESS | FAILURE | STARTED | PENDING | null (never run)
  last_run_status: string | null
  last_run_at: string | null
  // why this person cannot open it (a container registry they have no access to); null: it opens
  locked_reason?: string | null
}

export interface FlowDetail extends FlowSummary {
  definition: string // the canvas's serialized JSON: { nodes, edges }
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
  created_by_name: string | null
  is_current: boolean
}

export function useFlows() {
  const { apiFetch } = useApiClient()

  return {
    // all the flows in the readable projects (for the pickers; not paginated)
    list: () => apiFetch<FlowSummary[]>('/flows'),

    // global page: server-side search (over the whole dataset) + pagination
    listPaged: (p: { q?: string; limit: number; offset: number }) =>
      apiFetch<Page<FlowSummary>>(`/flows/search?${pagedQuery(p)}`),

    listByProject: (projectId: number) =>
      apiFetch<FlowSummary[]>(`/projects/${projectId}/flows`),

    get: (id: number) => apiFetch<FlowDetail>(`/flows/${id}`),

    create: (
      projectId: number,
      body: { name: string; description?: string; definition: string; engine?: string; production_engine?: string | null },
    ) =>
      apiFetch<FlowDetail>(`/projects/${projectId}/flows`, { method: 'POST', body }),

    update: (
      id: number,
      // production_engine: '' = back to the same as development
      body: Partial<{ name: string; description: string; definition: string; project_id: number; engine: string; production_engine: string }>,
    ) => apiFetch<FlowDetail>(`/flows/${id}`, { method: 'PATCH', body }),

    remove: (id: number) => apiFetch<void>(`/flows/${id}`, { method: 'DELETE' }),

    // a copy in the same folder, named «name (i)»
    duplicate: (id: number) => apiFetch<FlowDetail>(`/flows/${id}/duplicate`, { method: 'POST' }),

    // run statistics (number of runs, successes/failures, last, average time)
    stats: (id: number) => apiFetch<FlowStats>(`/flows/${id}/stats`),

    // the definition's version history + promotion of an old version
    versions: (id: number) => apiFetch<FlowVersionInfo[]>(`/flows/${id}/versions`),
    promote: (id: number, version: number) =>
      apiFetch<FlowDetail>(`/flows/${id}/versions/${version}/promote`, { method: 'POST' }),

    // sets/disables the scheduled run (5-field cron; '' = disable)
    // production_engine: omitted = unchanged; '' = as development
    setSchedule: (
      id: number,
      cron: string,
      production_engine?: string,
      notify?: { emails: string; connectionId: number },
    ) =>
      apiFetch<FlowDetail>(`/flows/${id}/schedule`, {
        method: 'PUT',
        body: {
          cron,
          ...(production_engine === undefined ? {} : { production_engine }),
          // omitted = unchanged; empty string and 0 = notice off
          ...(notify === undefined ? {} : { notify_emails: notify.emails, notify_connection_id: notify.connectionId }),
        },
      }),

    // runs the orchestration at once (refresh → output → runflow) in the background. mode
    // 'production' = with the production engine, as the scheduler would
    runNow: (id: number, mode: 'development' | 'production' = 'development') =>
      apiFetch<{ status: string; flow_id: number; run_id: number }>(`/flows/${id}/run-now?mode=${mode}`, {
        method: 'POST',
      }),
  }
}
