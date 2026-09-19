// Assistente AI: stato, modelli (pannello admin) e la chat in streaming.
//
// La chat arriva come Server-Sent Events su una POST: `$fetch` non li legge a
// pezzi, quindi qui si usa `fetch` con un lettore di stream. La conversazione
// e' salvata sul SERVER: a fine turno arriva l'id della chat, e la storia la
// rimanda al turno dopo.
import type { PreviewResult } from '~/composables/useApi'

export interface AiStatus {
  enabled: boolean
  models: string[]
  default_model: string | null
}

export interface AiModel {
  model_id: string
  enabled: boolean
  chat: boolean // false = embedding/audio/reranker: non abilitabile per la chat
  available: boolean // false = il provider non lo elenca piu'
}

export type AiTable = PreviewResult & { datasource?: string }

export interface AiToolCall {
  id: string
  name: 'list_datasources' | 'describe_datasource' | 'query_datasource' | string
  args: Record<string, any>
}

export interface AiToolResult {
  id: string
  name: string
  ok: boolean
  table?: AiTable
  count?: number
  error?: string
}

export interface AiChatHandlers {
  onText: (delta: string) => void
  onToolCall: (call: AiToolCall) => void
  onToolResult: (result: AiToolResult) => void
  onDone: (done: AiDone) => void
  onError: (message: string, code?: string) => void
}

export interface AiUsage {
  requests: number
  input_tokens: number
  output_tokens: number
  // costo in dollari COME LO RIPORTA pydantic-ai. Stringa decimale esatta;
  // null = non determinabile per quel modello, che NON vuol dire gratis.
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
}

export interface AiTurn {
  seq: number
  question: string
  answer: string
  steps: { name: string; args: any }[]
  model_id: string
  usage: AiUsage
  created_at: string
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
  // conversazione da proseguire; assente = nuova
  chat_id?: number | null
  engine?: string | null
  locale: string
  focus: number[]
}

export function useAi() {
  const { apiFetch } = useApiClient()
  const base = useRuntimeConfig().public.apiBase as string
  const token = useAuthToken()

  async function chat(body: AiChatBody, handlers: AiChatHandlers, signal?: AbortSignal): Promise<void> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'text/event-stream' }
    if (token.value) headers.Authorization = `Bearer ${token.value}`
    const resp = await fetch(`${base}/ai/chat`, { method: 'POST', headers, body: JSON.stringify(body), signal })
    if (resp.status === 401) {
      token.value = null
      navigateTo('/login')
      return
    }
    if (!resp.ok || !resp.body) {
      let detail = ''
      try { detail = (await resp.json())?.detail ?? '' } catch { /* corpo non JSON */ }
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
      // un evento SSE finisce con una riga vuota
      let cut = buffer.indexOf('\n\n')
      while (cut !== -1) {
        dispatch(buffer.slice(0, cut), handlers)
        buffer = buffer.slice(cut + 2)
        cut = buffer.indexOf('\n\n')
      }
    }
  }

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
    else if (event === 'error') h.onError(String(payload.message ?? ''), payload.code)
  }

  // le conversazioni salvate: sempre e solo quelle di chi chiede (il server non
  // ne restituisce di altri)
  const chats = () => apiFetch<AiChatSummary[]>('/ai/chats')
  const openChat = (id: number) => apiFetch<AiChatDetail>(`/ai/chats/${id}`)
  const removeChat = (id: number) => apiFetch<void>(`/ai/chats/${id}`, { method: 'DELETE' })

  return {
    status: () => apiFetch<AiStatus>('/ai/status'),
    models: () => apiFetch<AiModel[]>('/ai/models'),
    setModel: (modelId: string, enabled: boolean) =>
      apiFetch<AiModel>(`/ai/models/${encodeURIComponent(modelId)}`, { method: 'PUT', body: { enabled } }),
    chat,
    chats,
    openChat,
    removeChat,
  }
}
