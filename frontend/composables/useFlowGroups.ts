// Flow groups: a grid of steps kept in a folder like a flow. Each step is a row of
// cells run at the same time (a flow to run, or a database datasource to
// refresh); the rows run in order, and the first failed step stops the group.
//
// The gateway checks every cell when the grid is saved and again when it runs;
// `cells` in the detail is the label of each cell as the reader may see it.

export type FlowGroupCellKind = 'flow' | 'refresh' | 'script' | 'container'

export interface FlowGroupCell {
  kind: FlowGroupCellKind
  id: number
  /** a container cell's settings, as a Container node's data */
  container?: Record<string, any>
}

export interface FlowGroupDefinition {
  steps: (FlowGroupCell | null)[][]
}

export interface FlowGroupCellLabel {
  name: string | null
  project_id: number | null
  /** deleted since it was added */
  missing: boolean
  /** in a folder the reader cannot read: the name is not sent */
  hidden: boolean
}

export interface FlowGroup {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  definition: FlowGroupDefinition
  run_schedule: string | null
  run_scheduled_by: number | null
  next_run_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface FlowGroupDetail extends FlowGroup {
  cells: (FlowGroupCellLabel | null)[][]
  can_edit: boolean
  can_run: boolean
}

export type FlowGroupCellState = 'waiting' | 'running' | 'succeeded' | 'failed' | 'skipped'

export interface FlowGroupRunCell {
  kind: FlowGroupCellKind
  id: number
  state: FlowGroupCellState
  /** the flow's orchestration or the import that ran (or that the cell waited for) */
  run_id: number | null
  error: string | null
  /** the object was already running: the cell waited for that run */
  waited: boolean
}

export interface FlowGroupRun {
  id: number
  status: string
  trigger_type: string
  launched_by: number | null
  error: string | null
  started_at: string | null
  finished_at: string | null
  cells: (FlowGroupRunCell | null)[][]
}

export const MAX_GROUP_STEPS = 50
export const MAX_GROUP_COLUMNS = 20

export function useFlowGroups() {
  const { apiFetch } = useApiClient()

  return {
    list: () => apiFetch<FlowGroup[]>('/flow-groups'),

    listByProject: (projectId: number) => apiFetch<FlowGroup[]>(`/projects/${projectId}/flow-groups`),

    get: (id: number) => apiFetch<FlowGroupDetail>(`/flow-groups/${id}`),

    create: (projectId: number, body: { name: string; description?: string; definition: FlowGroupDefinition }) =>
      apiFetch<FlowGroupDetail>(`/projects/${projectId}/flow-groups`, { method: 'POST', body }),

    update: (id: number, body: { name?: string; description?: string; definition?: FlowGroupDefinition; project_id?: number }) =>
      apiFetch<FlowGroupDetail>(`/flow-groups/${id}`, { method: 'PATCH', body }),

    remove: (id: number) => apiFetch<void>(`/flow-groups/${id}`, { method: 'DELETE' }),

    duplicate: (id: number) => apiFetch<FlowGroupDetail>(`/flow-groups/${id}/duplicate`, { method: 'POST' }),

    runNow: (id: number) =>
      apiFetch<{ status: string; flow_group_id: number; run_id: number }>(`/flow-groups/${id}/run-now`, { method: 'POST' }),

    /** empty cron = not scheduled */
    setSchedule: (id: number, cron: string) =>
      apiFetch<FlowGroupDetail>(`/flow-groups/${id}/schedule`, { method: 'PUT', body: { cron } }),

    runs: (id: number) => apiFetch<FlowGroupRun[]>(`/flow-groups/${id}/runs`),
  }
}
