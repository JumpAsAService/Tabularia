// AI assistant: state, models (admin panel) and the streaming chat.
//
// The chat arrives as Server-Sent Events on a POST: `$fetch` does not read them in pieces,
// so here `fetch` with a stream reader is used. The conversation is saved on the SERVER:
// at the end of the turn the chat's id arrives, and the history sends it back at the
// next turn.
import type { PreviewResult } from '~/composables/useApi'
import type { AgentPlan } from '~/composables/useAgentPlans'

export interface AiStatus {
  enabled: boolean
  models: string[]
  default_model: string | null
}

export interface AiModel {
  model_id: string
  enabled: boolean
  chat: boolean // false = embedding/audio/reranker: cannot be enabled for the chat
  available: boolean // false = the provider no longer lists it
}

// an agent's instructions (Administration → AI): the text, editable by the administrators,
// and the context Tabularia adds at every request, shown with example values
export type AiAgent = 'assistant' | 'flow' | 'script' | 'title' | 'dbt_describe' | 'dbt_translate'

export interface AiPrompt {
  agent: AiAgent
  text: string
  default: string
  custom: boolean
  context: string
  updated_at: string | null
  updated_by: string | null
}

export type AiTable = PreviewResult & { datasource?: string }

export interface AiToolCall {
  id: string
  name: 'list_datasources' | 'describe_datasource' | 'query_datasource' | string
  args: Record<string, any>
  // the Assistant page's agent: the tool's title in the registry (English), for a tool the page has no words for
  title?: string
}

export interface AiChartSpec {
  type: 'bar' | 'hbar' | 'line' | 'area' | 'pie' | 'donut' | 'scatter'
  x: string
  y: string
  series?: string
  title?: string
}

export interface AiToolResult {
  id: string
  name: string
  ok: boolean
  table?: AiTable
  chart?: AiChartSpec
  count?: number
  error?: string
  // the flow editor's tools: the node made or changed, and a step whose preview failed
  node?: string
  warning?: string
  // how long the call took
  duration_ms?: number
  // the Assistant page's agent: the name of what the tool read or made, the plan it added to
  subject?: string
  plan_id?: number
}

/** A question of the Assistant page's agent (`ask_user`), with its choices: the first is the recommended one. */
export interface AgentQuestion {
  question: string
  options: { label: string; description: string; recommended: boolean }[]
}

// the answers' format, a line per question: the gateway reads it again (services/agent/questions.py)
export const ANSWER_ARROW = ' → '

/** The person's answers to a card, as their next message: «question → answer», a line each. */
export function answersMessage(questions: AgentQuestion[], answers: string[]): string {
  const line = (s: string) => s.replace(/\s+/g, ' ').trim()
  return questions.map((q, i) => `${line(q.question)}${ANSWER_ARROW}${line(answers[i] ?? '')}`).join('\n')
}

export interface AiChatHandlers {
  onText: (delta: string) => void
  onToolCall: (call: AiToolCall) => void
  onToolResult: (result: AiToolResult) => void
  onDone: (done: AiDone) => void
  // `extra`: what came with the error (the flow editor's assistant sends what it built before the stop)
  onError: (message: string, code?: string, extra?: Partial<AiDone>) => void
}

export interface AiUsage {
  requests: number
  input_tokens: number
  output_tokens: number
  // cost in dollars AS pydantic-ai REPORTS IT. An exact decimal string; null = cannot be
  // worked out for that model, which does NOT mean free.
  cost_usd: string | null
}

export interface AiChatTotal {
  turns: number
  input_tokens: number
  output_tokens: number
  cost_usd: string | null
}

export interface AiDone {
  chat_id: number
  seq: number | null
  usage: AiUsage
  chat_total: AiChatTotal | null
  // the flow editor's assistant: the new canvas (null when nothing changed) and the nodes it touched
  definition?: string | null
  touched?: string[]
  // the script editor's assistant: the files it proposes (null when it proposed none)
  proposal?: ScriptProposal | null
  // the Assistant page's agent: the questions that ended the turn, the plan it proposed or changed
  questions?: AgentQuestion[] | null
  plan?: AgentPlan | null
}

export interface ScriptProposal {
  files: { path: string; content: string }[]
  entrypoint: string
  changed: string[]
  removed: string[]
  summary: string
}

/** One turn of the script editor's assistant: no data goes, only the code, the
 *  environment, the names of the variables and columns, the error of a failed run. */
export interface ScriptAssistBody {
  message: string
  model: string
  chat_id?: number | null
  environment_id: number
  files: { path: string; content: string }[]
  entrypoint: string
  variables: string[]
  inputs: { variable: string; columns: { name: string; dtype: string }[] }[]
  output: 'required' | 'optional' | 'none'
  saved: boolean
  run_log?: string
  run_exit_code?: number | null
  locale: string
}

export interface FlowAssistBody {
  message: string
  model: string
  chat_id?: number | null
  flow_id: number | null
  // the canvas as the editor saves it
  definition: string
  engine?: string | null
  locale: string
  selected?: string | null
  // the editor's id counters, so a new node never takes back a deleted node's id
  counters?: Record<string, number>
}

export interface AiTurn {
  seq: number
  question: string
  answer: string
  steps: {
    id?: string; name: string; args: any; table?: AiTable; chart?: AiChartSpec; node?: string
    // how the call ended (false: refused, with the reason), how long it took, its words and result
    ok?: boolean; error?: string; duration_ms?: number; title?: string; subject?: string; plan_id?: number
  }[]
  model_id: string
  usage: AiUsage
  created_at: string
  // the Assistant page's agent: the questions that ended the turn and the answers the next
  // message gave (null: it said something else; missing: the last turn), the plan as it is now
  questions?: AgentQuestion[]
  answers?: (string | null)[] | null
  plan_id?: number
  plan?: AgentPlan
}

export interface AiChatSummary extends AiChatTotal {
  id: number
  title: string
  model_id: string
  engine: string | null
  created_at: string
  updated_at: string
}

export interface AiChatDetail extends AiChatSummary {
  messages: AiTurn[]
}

export interface AiChatBody {
  message: string
  model: string
  // the conversation to continue; missing = new
  chat_id?: number | null
  engine?: string | null
  locale: string
  focus: number[]
}

export function useAi() {
  const { apiFetch } = useApiClient()
  const base = useRuntimeConfig().public.apiBase as string

  async function stream(path: string, body: object, handlers: AiChatHandlers, signal?: AbortSignal): Promise<void> {
    const headers: Record<string, string> = { ...CSRF_HEADERS, 'Content-Type': 'application/json', Accept: 'text/event-stream' }
    const resp = await fetch(`${base}${path}`, {
      method: 'POST', headers, body: JSON.stringify(body), signal, credentials: 'include',
    })
    if (resp.status === 401) {
      dropSession()
      navigateTo('/login')
      return
    }
    if (!resp.ok || !resp.body) {
      let detail = ''
      try { detail = (await resp.json())?.detail ?? '' } catch { /* body not JSON */ }
      handlers.onError(String(detail || resp.statusText || resp.status), String(resp.status))
      return
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // an SSE event ends with an empty line
      let cut = buffer.indexOf('\n\n')
      while (cut !== -1) {
        dispatch(buffer.slice(0, cut), handlers)
        buffer = buffer.slice(cut + 2)
        cut = buffer.indexOf('\n\n')
      }
    }
  }

  const chat = (body: AiChatBody, handlers: AiChatHandlers, signal?: AbortSignal) => stream('/ai/chat', body, handlers, signal)
  /** One turn of the flow editor's assistant: it changes the canvas sent and returns it in `done`. */
  const flowAssist = (body: FlowAssistBody, handlers: AiChatHandlers, signal?: AbortSignal) =>
    stream('/ai/flow-assist', body, handlers, signal)
  /** One turn of the script editor's assistant: its proposal of files comes in `done`. */
  const scriptAssist = (body: ScriptAssistBody, handlers: AiChatHandlers, signal?: AbortSignal) =>
    stream('/ai/script-assist', body, handlers, signal)

  function dispatch(block: string, h: AiChatHandlers) {
    let event = 'message'
    let data = ''
    for (const line of block.split('\n')) {
      if (line.startsWith('event: ')) event = line.slice(7).trim()
      else if (line.startsWith('data: ')) data += line.slice(6)
    }
    if (!data) return
    let payload: any
    try { payload = JSON.parse(data) } catch { return }
    if (event === 'text') h.onText(String(payload.delta ?? ''))
    else if (event === 'tool_call') h.onToolCall(payload)
    else if (event === 'tool_result') h.onToolResult(payload)
    else if (event === 'done') h.onDone(payload as AiDone)
    else if (event === 'error') h.onError(String(payload.message ?? ''), payload.code, payload)
  }

  // the saved conversations: always and only the asker's (the server returns no others')
  const chats = () => apiFetch<AiChatSummary[]>('/ai/chats')
  const openChat = (id: number) => apiFetch<AiChatDetail>(`/ai/chats/${id}`)
  const removeChat = (id: number) => apiFetch<void>(`/ai/chats/${id}`, { method: 'DELETE' })

  /**
   * The WHOLE result of a step, not the rows shown: the server runs the saved query again
   * and streams the file.
   */
  const exportStep = (chatId: number, seq: number, stepId: string, fmt: 'csv' | 'xlsx') =>
    apiFetch<Blob>(
      `/ai/chats/${chatId}/turns/${seq}/steps/${encodeURIComponent(stepId)}/export?fmt=${fmt}`,
      { responseType: 'blob' },
    )

  return {
    status: () => apiFetch<AiStatus>('/ai/status'),
    models: () => apiFetch<AiModel[]>('/ai/models'),
    setModel: (modelId: string, enabled: boolean) =>
      apiFetch<AiModel>(`/ai/models/${encodeURIComponent(modelId)}`, { method: 'PUT', body: { enabled } }),
    prompts: (locale: string) => apiFetch<AiPrompt[]>(`/ai/prompts?locale=${encodeURIComponent(locale)}`),
    setPrompt: (agent: AiAgent, text: string, locale: string) =>
      apiFetch<AiPrompt>(`/ai/prompts/${agent}?locale=${encodeURIComponent(locale)}`, { method: 'PUT', body: { text } }),
    resetPrompt: (agent: AiAgent, locale: string) =>
      apiFetch<AiPrompt>(`/ai/prompts/${agent}?locale=${encodeURIComponent(locale)}`, { method: 'DELETE' }),
    chat,
    flowAssist,
    scriptAssist,
    /** The user's latest conversation with the flow editor's assistant about a flow, or null. */
    latestFlowChat: (flowId: number) => apiFetch<AiChatDetail | null>(`/ai/flow-chats/latest?flow_id=${flowId}`),
    chats,
    openChat,
    removeChat,
    exportStep,
  }
}
