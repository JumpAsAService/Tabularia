// Flow groups: a grid of steps kept in a folder like a flow. Each step is a row of
// cells run at the same time (a flow to run, a database datasource to refresh, a
// SQL script's datasource to write…); the rows run in order, and the first failed
// step stops the group.
//
// The gateway checks every cell when the grid is saved and again when it runs;
// `cells` in the detail is the label of each cell as the reader may see it.

export type FlowGroupCellKind = 'flow' | 'refresh' | 'script' | 'container' | 'pushmail' | 'sql'

/** A Pushmail cell: one email through an SMTP connection, one .xlsx per datasource,
 *  one PDF or Excel file per report (docs/specs/reports.md, «Sending»). */
export interface PushmailAttachment { datasourceId: number | null; fileName: string; maxRows: number }
/** A report's values as texts, as a link carries them: empty = the report's default when
 *  the email leaves; a date may be a placeholder ({start_of_month}). */
export type PushmailReportParams = Record<string, string | string[]>
export interface PushmailReport {
  reportId: number | null
  format: 'pdf' | 'xlsx'
  params: PushmailReportParams
  /** a template with the Outputs' placeholders, the extension added */
  fileName: string
  /** its summary (texts, KPIs, totals, charts) in the body, under the message: one per cell */
  inBody: boolean
}
export const PUSHMAIL_MAX_REPORTS = 5
export interface PushmailConfig {
  label: string
  connectionId: number | null
  to: string[]
  cc: string[]
  subject: string
  body: string
  bodyIsHtml: boolean
  timezone: string
  attachments: PushmailAttachment[]
  /** absent in a cell saved before reports could be sent */
  reports?: PushmailReport[]
}

export interface FlowGroupCell {
  kind: FlowGroupCellKind
  id: number
  /** a container cell's settings, as a Container node's data */
  container?: Record<string, any>
  /** a pushmail cell's settings (components/containers/PushmailCellPanel.vue) */
  pushmail?: PushmailConfig
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
