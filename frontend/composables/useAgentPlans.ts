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
}

export interface AgentPlanAction {
  n: number
  kind: 'save_flow' | 'run_flow' | 'schedule_flow' | string
  words: string
  name?: string
  folder?: string
  engine?: string
  flow_id?: number | null
  draft_id?: number | null
  mode?: 'development' | 'production'
  cron?: string | null
  timezone?: string
  notify_emails?: string | null
  node_lines?: string[] // a new flow: its nodes
  diff_lines?: string[] // a changed flow: what changes
  outputs?: AgentPlanOutput[]
  preview?: { node: string; columns?: { name: string; dtype?: string }[]; rows?: Record<string, unknown>[]; error?: string }
}

export interface AgentPlanResult {
  ok: boolean
  words?: string
  link?: string
  error?: string
  skipped?: boolean
  object?: { kind: string; id: number; name?: string }
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
}

export function useAgentPlans() {
  const { apiFetch } = useApiClient()
  return {
    get: (id: number) => apiFetch<AgentPlan>(`/agent/plans/${id}`),
    confirm: (id: number) => apiFetch<AgentPlan>(`/agent/plans/${id}/confirm`, { method: 'POST' }),
    cancel: (id: number) => apiFetch<AgentPlan>(`/agent/plans/${id}/cancel`, { method: 'POST' }),
  }
}
