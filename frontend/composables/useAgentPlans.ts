// The agent's plans (docs/specs/mcp-agent.md): what an agent proposes to change — in the
// chat or over MCP — shown to its owner, who confirms or cancels it. Nothing changes
// before the confirmation; the gateway checks the permissions again when it applies.

export type AgentPlanStatus = 'pending' | 'applying' | 'applied' | 'failed' | 'cancelled' | 'expired'

export interface AgentPlanOutput {
  node: string
  kind: 'datasource' | 'database' | 's3' | 'sftp' | 'email'
  words: string // in English, for the model
  target: string
  folder?: string
  connection?: string
  format?: string
  mode?: 'append' | 'replace'
  overwrite?: boolean
  subject?: string
  cc?: string
  // database: the SQL run on the database after the write, at every run
  post_sql?: string
  // email: the message, as it is sent
  body?: string
  body_is_html?: boolean
}

export type AgentPlanActionKind =
  | 'save_flow' | 'run_flow' | 'schedule_flow'
  | 'create_datasource' | 'refresh_datasource' | 'schedule_datasource'
  | 'save_sql_script' | 'run_sql_script' | 'schedule_sql_script'
  | 'save_flow_group' | 'run_flow_group' | 'schedule_flow_group'
  | 'save_notebook'

export interface AgentPlanAction {
  n: number
  kind: AgentPlanActionKind | string
  words: string
  name?: string
  folder?: string
  engine?: string | null
  // the saved object a save_* action changes (null: it creates one)
  id?: number | null
  flow_id?: number | null
  draft_id?: number | null
  mode?: 'development' | 'production'
  cron?: string | null
  timezone?: string
  notify_emails?: string | null
  node_lines?: string[] // a new flow: its nodes; a new flow group: its cells
  diff_lines?: string[] // a changed flow, SQL script, flow group or notebook: what changes
  outputs?: AgentPlanOutput[]
  preview?: { node?: string; columns?: { name: string; dtype?: string }[]; rows?: Record<string, unknown>[]; error?: string }
  // datasources: the connection they import from, and what
  connection?: string | null
  connection_type?: string
  source_type?: 'table' | 'sql' | 's3' | 'sftp' | 'sharepoint' | 'api'
  // the table, the SQL, the files' pattern, or the API call whole (method, path, headers, body)
  source_text?: string
  // SQL scripts: the datasource they write
  output?: { name: string; folder: string } | null
  // a new SQL script's SQL, a new notebook's source: whole, it is what will run
  code?: string
  environment?: string | null // notebooks
}

export interface AgentPlanResult {
  ok: boolean
  words?: string
  link?: string
  error?: string
  skipped?: boolean
  object?: { kind: string; id: number; name?: string; [key: string]: unknown }
}

export interface AgentPlan {
  id: number
  origin: 'chat' | 'mcp'
  token_name: string | null
  chat_id: number | null
  status: AgentPlanStatus
  summary: string
  actions: AgentPlanAction[]
  result: AgentPlanResult[] | null
  created_at: string
  expires_at: string
  applied_at: string | null
  can_confirm: boolean
  // the actions as shown: confirming sends it back, and a plan the agent changed since is refused (409)
  revision: string
}

export function useAgentPlans() {
  const { apiFetch } = useApiClient()
  return {
    get: (id: number) => apiFetch<AgentPlan>(`/agent/plans/${id}`),
    // the plan as the person saw it: a plan changed since is not applied
    confirm: (plan: Pick<AgentPlan, 'id' | 'revision'>) =>
      apiFetch<AgentPlan>(`/agent/plans/${plan.id}/confirm`, { method: 'POST', body: { revision: plan.revision } }),
    cancel: (id: number) => apiFetch<AgentPlan>(`/agent/plans/${id}/cancel`, { method: 'POST' }),
  }
}
