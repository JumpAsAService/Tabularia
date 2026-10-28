// The steps of the Assistant page's agent (docs/specs/mcp-agent.md, «visible work»): each
// tool call is a step — what it does in words, its arguments on demand, its state, how long
// it took, its result — and the calls of one tool that failed and were tried again sit under
// the same step, with the reason of each failure.
import { useI18n } from 'vue-i18n'
import type { AiChartSpec, AiTable } from '~/composables/useAi'

export interface AgentStep {
  id: string
  name: string
  // the registry's title, in English: the words for a tool the page has none for
  title?: string
  args: Record<string, any>
  state: 'running' | 'ok' | 'error'
  error?: string
  count?: number
  table?: AiTable
  chart?: AiChartSpec
  node?: string
  // the name of what the tool read or made (a flow, a draft)
  subject?: string
  planId?: number
  durationMs?: number
}

/** A step as the page shows it: its last attempt, and the attempts that failed before it. */
export interface AgentStepGroup {
  key: string
  last: AgentStep
  failed: AgentStep[]
}

/** The calls in order → the steps: a call of the same tool right after one that failed is
 *  its retry. Two calls in parallel (none failed yet) stay two steps. */
export function groupSteps(steps: AgentStep[]): AgentStepGroup[] {
  const out: AgentStepGroup[] = []
  for (const s of steps) {
    const prev = out[out.length - 1]
    if (prev && prev.last.name === s.name && prev.last.state === 'error') {
      prev.failed.push(prev.last)
      prev.last = s
    } else {
      out.push({ key: s.id, last: s, failed: [] })
    }
  }
  return out
}

// tool arguments come back from a saved conversation as text or as an object
export function argsOf(raw: unknown): Record<string, any> {
  if (raw && typeof raw === 'object') return raw as Record<string, any>
  try {
    return JSON.parse(String(raw ?? '{}')) ?? {}
  } catch {
    return {}
  }
}

export function useAgentSteps() {
  const { t, te, locale } = useI18n()

  /** What the step does, in words. `dsName` names a datasource by its id. */
  function label(s: AgentStep, dsName: (id: unknown) => string): string {
    const a = s.args ?? {}
    const id = (v: unknown) => (v == null ? '…' : `#${v}`)
    switch (s.name) {
      case 'list_datasources': return s.state === 'ok' ? t('chat.stepListDone', { n: s.count ?? 0 }) : t('chat.stepList')
      case 'describe_datasource': return t('chat.stepDescribe', { name: dsName(a.datasource_id) })
      case 'query_datasource': return t('chat.stepQuery', { name: dsName(a.datasource_id) })
      case 'search': return t('chat.tool.search', { q: a.query ?? '' })
      case 'get_flow': return t('chat.tool.get_flow', { name: s.subject ?? id(a.flow_id) })
      case 'get_run': return t('chat.tool.get_run', { id: id(a.run_id) })
      case 'get_plan': return t('chat.tool.get_plan', { id: id(a.plan_id) })
      case 'cancel_plan': return t('chat.tool.cancel_plan', { id: id(a.plan_id) })
      case 'plan_schedule': return a.cron ? t('chat.tool.plan_schedule', { cron: a.cron }) : t('chat.tool.plan_schedule_off')
      case 'flow_draft_new': return t('chat.tool.flow_draft_new', { name: a.name ?? '' })
      case 'flow_draft_open': return t('chat.tool.flow_draft_open', { name: s.subject ?? id(a.flow_id) })
      case 'flow_add_source': return t('chat.tool.flow_add_source', { name: dsName(a.datasource_id) })
      case 'flow_add_step': return t('chat.tool.flow_add_step', { op: a.operation ?? '…' })
      case 'flow_change_step': return t('chat.tool.flow_change_step', { node: a.node ?? '…' })
      case 'flow_connect': return t('chat.tool.flow_connect', { from: a.from_node ?? '…', to: a.to_node ?? '…' })
      case 'flow_remove_node': return t('chat.tool.flow_remove_node', { node: a.node ?? '…' })
      case 'flow_add_output': return t('chat.tool.flow_add_output', { kind: a.kind ?? '…' })
      case 'flow_preview': return t('chat.tool.flow_preview', { node: a.node ?? '…' })
      default:
        // a tool without its own words: plain ones (list_folders…), or the registry's title
        return te(`chat.tool.${s.name}`) ? t(`chat.tool.${s.name}`) : (s.title ?? s.name)
    }
  }

  /** The step's result in a few words, beside its label (a table is shown below instead). */
  function result(s: AgentStep): string {
    if (s.state !== 'ok' || s.table) return ''
    if (s.planId != null) return t('chat.step.plan', { id: s.planId })
    if (s.node) return t('chat.step.node', { node: s.node })
    if (s.count != null && s.name !== 'list_datasources') return t('chat.step.found', { n: s.count })
    return ''
  }

  function duration(ms: number | undefined): string {
    if (ms == null) return ''
    if (ms < 1000) return t('chat.step.ms', { ms: Math.max(0, Math.round(ms)) })
    const s = new Intl.NumberFormat(locale.value, { maximumFractionDigits: ms < 10_000 ? 1 : 0 }).format(ms / 1000)
    return t('chat.step.seconds', { s })
  }

  return { label, result, duration }
}
