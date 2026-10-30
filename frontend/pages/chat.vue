<script setup lang="ts">
// AI assistant: a conversation INSIDE the user's catalog. On the left the datasources
// they can read (the same the assistant can query), in the middle questions, the
// assistant's steps and each query's table: the answer's numbers are checked right
// below.
//
// The assistant also BUILDS (docs/specs/mcp-agent.md): it asks what it is not sure about
// with a card of choices, and before changing anything it proposes a plan, shown as a
// card that only the person's «Confirm» applies. Every tool call is a step, with its retries
// grouped under it.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  Sparkles, Send, Square, Plus, Database, Search, Check, FileText,
  CircleAlert, ChevronRight, ShieldAlert, ShieldCheck, History, Trash2, Download,
} from 'lucide-vue-next'
import { errMessage, useApi } from '~/composables/useApi'
import MiniChart from '~/components/ui/MiniChart.vue'
import AiDisclosure from '~/components/ui/AiDisclosure.vue'
import { useAiDisclosure } from '~/composables/useAiDisclosure'
import { useAi, type AgentQuestion, type AiChatSummary, type AiChatTotal, type AiStatus, type AiToolCall, type AiToolResult, type AiUsage } from '~/composables/useAi'
import { argsOf, type AgentStep } from '~/composables/useAgentSteps'
import type { AgentPlan } from '~/composables/useAgentPlans'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useToast } from '~/composables/useToast'
import { useProjects } from '~/composables/useProjects'
import { usePreferredEngine, type EngineOpt } from '~/composables/useEngine'
import { useLocale } from '~/composables/useLocale'
// explicit imports: in the container new components are not scanned
import AppShell from '~/components/AppShell.vue'
import DataGrid from '~/components/ui/DataGrid.vue'
import MarkdownLite from '~/components/ui/MarkdownLite.vue'
import AiSpeaker from '~/components/ui/AiSpeaker.vue'
import Select from '~/components/ui/Select.vue'
import AgentSteps from '~/components/agent/AgentSteps.vue'
import AgentQuestionCard from '~/components/agent/AgentQuestionCard.vue'
import AgentPlanCard from '~/components/agent/AgentPlanCard.vue'

const { t } = useI18n()
// A chat without a title yet (or with the Italian default written before 1.2.0)
// is «New conversation» in the language of the interface.
function chatTitle(title: string): string {
  return !title || title === 'Nuova conversazione' ? t('chat.newConversation') : title
}
const toast = useToast()
const { user } = useAuth()
// Observer and not administrator: for them the conversation is saved nowhere (a shared
// account on an open installation). The same condition as the gateway: `is_observer`
// from /auth/me is true for admins too.
const noHistory = computed(() => !!user.value?.is_observer && !user.value?.is_superuser)
const ai = useAi()
const dsApi = useDatasources()
const projectsApi = useProjects()
const api = useApi()
// the queries' engine is CHOSEN, among those available and allowed by the
// administrator: the user's favourite is only the starting value
const { defaultEngine } = usePreferredEngine()
const engines = ref<EngineOpt[]>([])
const engine = ref('')
const { locale } = useLocale()

type Step = AgentStep
type Turn =
  | { role: 'user'; text: string }
  | {
      role: 'assistant'; text: string; steps: Step[]; streaming: boolean; error?: string; usage?: AiUsage; phase?: Phase
      phaseOn?: string; seq?: number; model?: string
      // the questions that ended the turn, and the answers given (null: none — see AgentQuestionCard)
      questions?: AgentQuestion[]; answers?: (string | null)[] | null
      // the plan the turn proposed or changed, as it is now
      plan?: AgentPlan
    }
type BotTurn = Extract<Turn, { role: 'assistant' }>

// What the assistant is doing at this instant. It is NOT decoration: every phase matches
// a real event of the stream, so the indicator tells the truth as the rest of the page
// does («every step can be inspected»).
type Phase = 'thinking' | 'catalog' | 'fields' | 'query' | 'building' | 'planning' | 'writing'
function phaseOf(tool: string): Phase {
  if (tool === 'describe_datasource') return 'fields'
  if (tool === 'query_datasource') return 'query'
  if (tool.startsWith('flow_')) return 'building'
  if (tool.startsWith('plan_') || tool === 'cancel_plan') return 'planning'
  return 'catalog'
}

const status = ref<AiStatus | null>(null)
const statusError = ref('')
const model = ref('')
const datasources = ref<DatasourceInfo[]>([])
const folders = ref<Record<number, string>>({})
const filter = ref('')
const focus = ref<number[]>([])
const turns = ref<Turn[]>([])
// the history is on the SERVER: here the open conversation's id is enough
const chatId = ref<number | null>(null)
const chats = ref<AiChatSummary[]>([])
const chatsOpen = ref(false)
const chatFilter = ref('')
const chatTotal = ref<AiChatTotal | null>(null)
const draft = ref('')
const busy = ref(false)
// «Change» on a plan: the field says what to write
const changeHint = ref(false)
let abort: AbortController | null = null
const scroller = ref<HTMLElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
const chatSearch = ref<HTMLInputElement | null>(null)
const nf = new Intl.NumberFormat()

// the search looks at the title: it is the only thing the list shows, and searching
// inside conversations not loaded would say «found» without being able to show where
const chatsFiltered = computed(() => {
  const q = chatFilter.value.trim().toLowerCase()
  if (!q) return chats.value
  const parole = q.split(/\s+/)
  return chats.value.filter((c) => {
    const t = chatTitle(c.title).toLowerCase()
    return parole.every((w) => t.includes(w))
  })
})

// Informed consent: it holds for ANYONE, observers included — for them above all,
// because on a shared account every visitor is a different person and must be informed
// on their own (that is why it is remembered in the browser and not on the user). It
// goes into `ready`, so everything that respects it already — box, send, examples,
// choice of model and engine — stays blocked by itself.
const { accepted, read: readConsent, accept: acceptConsent } = useAiDisclosure()
const ready = computed(() =>
  !!status.value?.enabled && !!status.value.models.length && accepted.value === true)
// blocked by the consent, not by something missing: it avoids false messages
const awaitingConsent = computed(() =>
  accepted.value !== true && !!status.value?.enabled && !!status.value.models.length)
const modelOptions = computed(() => (status.value?.models ?? []).map((m) => ({ value: m, label: m })))
const engineOptions = computed(() => engines.value.filter((e) => e.available).map((e) => ({ value: e.id, label: e.label || e.id })))
const shown = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const list = datasources.value.filter((d) => d.key)
  return q ? list.filter((d) => d.name.toLowerCase().includes(q) || (d.description || '').toLowerCase().includes(q)) : list
})
// the SERVER makes the count: the «documented» rule lives in one place only
const described = (d: DatasourceInfo) => d.described_columns
const dsName = (id: any) => datasources.value.find((d) => d.id === Number(id))?.name ?? `#${id}`
// the fourth asks to build: not offered to whom only reads
const metadataOnly = computed(() => !!status.value?.metadata_only)
const examples = computed(() => [t('chat.example1'),
  ...(metadataOnly.value ? [t('chat.metadataExample2'), t('chat.metadataExample3')] : [t('chat.example2'), t('chat.example3')]),
  ...(noHistory.value ? [] : [t('chat.example4')])])

onMounted(async () => {
  try {
    status.value = await ai.status()
    model.value = status.value.default_model ?? status.value.models[0] ?? ''
  } catch (e) {
    statusError.value = errMessage(e)
  }
  // after the state: the dialog's text depends on it
  readConsent(metadataOnly.value)
  try {
    // a FRESH catalog (not the session's cached one): the engines' policy may have changed
    engines.value = await api.engines()
    engine.value = defaultEngine(engines.value)
  } catch { /* without a catalog the backend decides */ }
  try {
    const [list, projects] = await Promise.all([dsApi.list(), projectsApi.list()])
    datasources.value = list.sort((a, b) => a.name.localeCompare(b.name))
    folders.value = Object.fromEntries(projects.map((p: any) => [p.id, p.name]))
  } catch { /* the catalog is a help: the chat works without it too */ }
  await refreshChats()
  input.value?.focus()
})
onBeforeUnmount(() => abort?.abort())

/** The phase's label: a true sentence, not background noise. */
function activityLabel(turn: BotTurn): string {
  const on = turn.phaseOn
  switch (turn.phase) {
    case 'catalog': return t('chat.actCatalog')
    case 'fields': return on ? t('chat.actFieldsOn', { name: on }) : t('chat.actFields')
    case 'query': return on ? t('chat.actQueryOn', { name: on }) : t('chat.actQuery')
    case 'building': return t('chat.actBuilding')
    case 'planning': return t('chat.actPlanning')
    case 'writing': return t('chat.actWriting')
    default: return t('chat.actThinking')
  }
}

function toggleFocus(id: number) {
  focus.value = focus.value.includes(id) ? focus.value.filter((x) => x !== id) : [...focus.value, id].slice(-8)
}

// ── the cards: questions and plans ──────────────────────────────────────────
const lastBot = computed(() => {
  for (let i = turns.value.length - 1; i >= 0; i--) if (turns.value[i].role === 'assistant') return i
  return -1
})
/** A card of questions takes answers only under the latest answer, once, and not while another
 *  answer is being written (its own turn is still closing when the card arrives). */
function questionsLocked(ti: number, turn: BotTurn): boolean {
  return (busy.value && !turn.streaming) || ti !== lastBot.value || !!turn.answers?.some((a) => a != null)
}
function answerCard(turn: BotTurn, message: string, answers: string[]) {
  if (busy.value || !ready.value) return
  turn.answers = answers
  void send(message)
}
// a plan changed in several turns: in full only under the latest of them
const planShownAt = computed(() => {
  const at = new Map<number, number>()
  turns.value.forEach((turn, i) => { if (turn.role === 'assistant' && turn.plan) at.set(turn.plan.id, i) })
  return at
})
function onPlanUpdate(plan: AgentPlan) {
  for (const turn of turns.value) if (turn.role === 'assistant' && turn.plan?.id === plan.id) turn.plan = plan
}
function onPlanChange() {
  changeHint.value = true
  input.value?.focus()
}

async function scrollDown() {
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

async function send(text?: string) {
  const message = (text ?? draft.value).trim()
  if (!message || busy.value || !ready.value) return
  draft.value = ''
  changeHint.value = false
  turns.value.push({ role: 'user', text: message })
  const reply: Turn = { role: 'assistant', text: '', steps: [], streaming: true, phase: 'thinking', model: model.value }
  turns.value.push(reply)
  const live = turns.value[turns.value.length - 1] as BotTurn
  busy.value = true
  abort = new AbortController()
  scrollDown()
  try {
    await ai.chat(
      { message, model: model.value, chat_id: chatId.value, engine: engine.value || null, locale: locale.value, focus: focus.value },
      {
        onText: (delta) => { live.text += delta; live.phase = 'writing'; scrollDown() },
        onToolCall: (call: AiToolCall) => {
          // a sentence, then a tool, then more text: what follows is a new paragraph
          if (live.text && !live.text.endsWith('\n\n')) live.text += '\n\n'
          live.steps.push({ id: call.id, name: call.name, title: call.title, args: call.args ?? {}, state: 'running' })
          live.phase = phaseOf(call.name)
          const parentId = dsName((call.args as any)?.datasource_id)
          live.phaseOn = parentId.startsWith('#') ? undefined : parentId
          scrollDown()
        },
        onToolResult: (res: AiToolResult) => {
          const step = live.steps.find((s) => s.id === res.id) ?? live.steps[live.steps.length - 1]
          if (!step) return
          step.state = res.ok ? 'ok' : 'error'
          step.error = res.error ?? res.warning
          step.count = res.count
          step.table = res.table
          step.chart = res.chart
          step.node = res.node
          step.subject = res.subject
          step.planId = res.plan_id
          step.durationMs = res.duration_ms
          live.phase = 'thinking'  // tool finished: back to reasoning
          scrollDown()
        },
        onDone: (done) => {
          chatId.value = done.chat_id
          chatTotal.value = done.chat_total
          live.usage = done.usage
          if (done.seq !== null) live.seq = done.seq
          if (done.questions?.length) live.questions = done.questions
          if (done.plan) live.plan = done.plan
          void refreshChats()
        },
        onError: (msg, code, extra) => {
          live.error = msg
          // the mode came on during this turn: the page says so from now on, and its dialog asks again
          if (code === 'metadata_only') ai.status().then((s) => { status.value = s; readConsent(metadataOnly.value) }).catch(() => {})
          // a turn stopped halfway may have opened the conversation and proposed a plan already
          if (extra?.chat_id) chatId.value = extra.chat_id
          if (extra?.plan) live.plan = extra.plan
        },
      },
      abort.signal,
    )
  } catch (e: any) {
    if (e?.name !== 'AbortError') live.error = errMessage(e)
  } finally {
    live.streaming = false
    live.steps.forEach((s) => { if (s.state === 'running') s.state = 'error' })
    busy.value = false
    abort = null
    scrollDown()
    // a card of questions takes the focus itself
    if (!live.questions?.length) input.value?.focus()
  }
}

function stop() { abort?.abort() }
function reset() {
  abort?.abort()
  turns.value = []
  chatId.value = null
  chatTotal.value = null
  changeHint.value = false
  input.value?.focus()
}

function toggleChats() {
  chatsOpen.value = !chatsOpen.value
  if (!chatsOpen.value) return
  // the title of a conversation just born is written by the model right after the answer:
  // reading the list again here it is always the updated one
  void refreshChats()
  nextTick(() => chatSearch.value?.focus())
}

// ── saved conversations ───────────────────────────────────────────────────
async function refreshChats() {
  if (noHistory.value) return   // there is no history to ask for
  try { chats.value = await ai.chats() } catch { /* the list is not essential */ }
}

/** Reopens a conversation: the questions asked already are not paid again. */
async function openChat(id: number) {
  if (busy.value) return
  try {
    const d = await ai.openChat(id)
    turns.value = d.messages.flatMap((t) => ([
      { role: 'user', text: t.question } as Turn,
      {
        role: 'assistant', text: t.answer, streaming: false, usage: t.usage, seq: t.seq, model: t.model_id,
        steps: t.steps.map((p, i): Step => ({
          id: p.id ?? `${t.seq}-${i}`, name: p.name, title: p.title, args: argsOf(p.args),
          state: p.ok === false ? 'error' : 'ok', error: p.error, table: p.table, chart: p.chart, node: p.node,
          subject: p.subject, planId: p.plan_id, durationMs: p.duration_ms,
        })),
        questions: t.questions, answers: t.answers, plan: t.plan,
      } as Turn,
    ]))
    chatId.value = d.id
    changeHint.value = false
    chatTotal.value = { turns: d.turns, input_tokens: d.input_tokens, output_tokens: d.output_tokens, cost_usd: d.cost_usd }
    if (d.model_id && (status.value?.models ?? []).includes(d.model_id)) model.value = d.model_id
    if (d.engine) engine.value = d.engine
    chatsOpen.value = false
    scrollDown()
  } catch (e: any) {
    toast.error(errMessage(e))
  }
}

async function removeChat(id: number) {
  if (!confirm(t('chat.confirmDelete'))) return
  try {
    await ai.removeChat(id)
    if (chatId.value === id) reset()
    await refreshChats()
  } catch (e: any) {
    toast.error(errMessage(e))
  }
}

const grabbing = ref('')

/**
 * Downloads a step's COMPLETE result: the chat shows at most 200 rows of it, the file
 * holds them all because the server runs the saved query again.
 */
async function grab(turn: BotTurn, step: Step, fmt: 'csv' | 'xlsx') {
  if (!chatId.value || turn.seq === undefined || grabbing.value) return
  grabbing.value = `${step.id}:${fmt}`
  try {
    const blob = await ai.exportStep(chatId.value, turn.seq, step.id, fmt)
    const fileName = `${step.table?.datasource || 'result'}.${fmt}`.toLowerCase().replace(/[^a-z0-9._-]+/g, '_')
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fileName
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: any) {
    toast.error(errMessage(e))
  } finally {
    grabbing.value = ''
  }
}

/**
 * The conversation as Markdown: questions, answers, and for every step the query that
 * produced it. It is the format one reads again in six months and pastes into a ticket,
 * not a screenshot.
 */
function downloadChat() {
  const rows: string[] = [
    `# ${chatTitle(chats.value.find((c) => c.id === chatId.value)?.title ?? '')}`, '',
    `> ${t('aiCompliance.exportNotice')}`, '',
  ]
  for (const turn of turns.value) {
    if (turn.role === 'user') { rows.push(`## ${t('aiCompliance.youLabel')}: ${turn.text}`, ''); continue }
    rows.push(`**${t('aiCompliance.assistantLabel')}${turn.model ? ` · ${turn.model}` : ''}**`, '')
    for (const s of turn.steps) {
      const sql = (s.args as any)?.sql
      if (sql) rows.push('```sql', String(sql), '```', '')
      if (s.table) {
        const cols = s.table.columns.map((c) => c.name)
        rows.push(`| ${cols.join(' | ')} |`, `|${cols.map(() => '---').join('|')}|`)
        for (const r of s.table.rows) rows.push(`| ${cols.map((c) => String((r as any)[c] ?? '')).join(' | ')} |`)
        rows.push('')
      }
    }
    if (turn.text) rows.push(turn.text, '')
    if (turn.questions?.length) {
      turn.questions.forEach((q, i) => rows.push(`- ${q.question}${turn.answers?.[i] ? ` → ${turn.answers[i]}` : ''}`))
      rows.push('')
    }
    if (turn.plan && planShownAt.value.get(turn.plan.id) === turns.value.indexOf(turn)) {
      rows.push(`**${t('chat.plan.title', { id: turn.plan.id })} — ${t(`agentPlan.status.${turn.plan.status}`)}**`, '')
      turn.plan.actions.forEach((a) => rows.push(`${a.n}. ${a.words}`))
      rows.push('')
    }
    if (turn.usage) rows.push(`*${money(turn.usage.cost_usd)} · ${t('chat.tokens', { i: nf.format(turn.usage.input_tokens), o: nf.format(turn.usage.output_tokens) })}*`, '')
  }
  const blob = new Blob([rows.join('\n')], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(chats.value.find((c) => c.id === chatId.value)?.title || 'conversation').replace(/[^a-z0-9._-]+/gi, '_')}.md`.toLowerCase()
  a.click()
  URL.revokeObjectURL(url)
}

/**
 * The cost as the model reports it, written in the user's language.
 * `null` = cannot be worked out, which is not zero: better a dash than a false «free».
 * Under a microdollar it says «less than», because `2e-7` in an interface is not a
 * number, it is the formatter's output.
 */
const MIN_COST = 0.000001
function money(v: string | null | undefined): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!isFinite(n)) return '—'
  const fmtUsd = (x: number, digits: number) =>
    new Intl.NumberFormat(locale.value, {
      style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: digits,
    }).format(x)
  if (n === 0) return fmtUsd(0, 2)
  if (n < MIN_COST) return t('chat.costUnder', { v: fmtUsd(MIN_COST, 6) })
  return fmtUsd(n, n < 0.01 ? 6 : 2)
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send() }
}
// the field grows with the text, up to a cap
watch(draft, async () => {
  await nextTick()
  const el = input.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 160)}px`
})
</script>

<template>
  <AppShell fluid>
    <!-- the gate: the chat is not used until one has read who one is talking to -->
    <AiDisclosure :open="accepted === false" :metadata-only="metadataOnly" @accept="acceptConsent" />
    <div class="chat">
      <!-- ── catalog: what the assistant can read ───────────────── -->
      <details class="rail" open>
        <summary class="rail-head">
          <Database :size="14" />
          <span>{{ $t('chat.railTitle', { n: shown.length }) }}</span>
        </summary>
        <p class="muted rail-lead">{{ $t('chat.railLead') }}</p>
        <label class="rail-search">
          <Search :size="13" />
          <input v-model="filter" type="search" :placeholder="$t('chat.railFilter')" :aria-label="$t('chat.railFilter')" />
        </label>
        <ul class="rail-list">
          <li v-for="d in shown" :key="d.id">
            <button class="ds" :class="{ on: focus.includes(d.id) }" type="button" :aria-pressed="focus.includes(d.id)" @click="toggleFocus(d.id)">
              <span class="ds-name">
                <Check v-if="focus.includes(d.id)" :size="13" class="ds-check" />
                {{ d.name }}
              </span>
              <span v-if="d.description" class="ds-about" :title="d.description">{{ d.description }}</span>
              <span class="ds-meta">
                <span>{{ folders[d.project_id] ?? '' }}</span>
                <span v-if="d.rows != null" class="num">{{ $t('chat.dsRows', { n: nf.format(d.rows) }) }}</span>
                <!-- documented in full: the same thing the catalog marks «ready for the AI», said here in
                     compact form -->
                <span v-if="d.ai_ready" class="ds-ready" :title="$t('datasources.aiReadyHint')">
                  <Sparkles :size="11" />
                </span>
                <span v-else-if="described(d)" class="ds-doc" :title="$t('chat.dsDescribed', { n: described(d), total: d.total_columns })">
                  <FileText :size="11" /> {{ described(d) }}/{{ d.total_columns }}
                </span>
              </span>
            </button>
          </li>
          <li v-if="!shown.length" class="muted rail-empty">{{ datasources.length ? $t('chat.railNoMatch') : $t('chat.railEmpty') }}</li>
        </ul>
      </details>

      <!-- ── conversation ─────────────────────────────────────────────── -->
      <section class="talk">
        <header class="talk-head">
          <h1><Sparkles :size="18" /> {{ $t('chat.pageTitle') }}</h1>
          <span v-if="chatTotal && chatTotal.turns" class="total" :title="$t('chat.costHint')">
            {{ $t('chat.chatTotal', { c: money(chatTotal.cost_usd) }) }}
          </span>
          <button v-if="!noHistory" class="mini" type="button" :aria-expanded="chatsOpen" @click="toggleChats">
            <History :size="13" /> {{ $t('chat.history', { n: chats.length }) }}
          </button>
          <span v-else class="nohistory" :title="$t('chat.historyUnavailableWhy')">
            <History :size="13" aria-hidden="true" /> {{ $t('chat.historyUnavailable') }}
          </span>
          <button v-if="turns.length" class="mini" type="button" :title="$t('chat.downloadChat')" @click="downloadChat">
            <Download :size="13" /> {{ $t('chat.downloadChatShort') }}
          </button>
          <button v-if="turns.length" class="mini" type="button" @click="reset"><Plus :size="13" /> {{ $t('chat.newConversation') }}</button>
        </header>

        <div v-if="chatsOpen" class="chats">
          <div class="chatsearch">
            <Search :size="14" />
            <input
              ref="chatSearch"
              v-model="chatFilter"
              type="search"
              :placeholder="$t('chat.searchChats')"
              :aria-label="$t('chat.searchChats')"
            >
            <span v-if="chatFilter" class="muted small count">{{ chatsFiltered.length }}/{{ chats.length }}</span>
          </div>
          <p v-if="!chats.length" class="muted small">{{ $t('chat.historyEmpty') }}</p>
          <p v-else-if="!chatsFiltered.length" class="muted small">{{ $t('chat.searchNoResults', { q: chatFilter }) }}</p>
          <ul v-else>
            <li v-for="c in chatsFiltered" :key="c.id" :class="{ sel: c.id === chatId }">
              <button
                type="button"
                class="pick"
                :disabled="busy"
                :aria-current="c.id === chatId ? 'true' : undefined"
                @click="openChat(c.id)"
              >
                <span class="t">{{ chatTitle(c.title) }}</span>
                <span class="muted small">{{ $t('chat.turns', { n: c.turns }) }} · {{ money(c.cost_usd) }}</span>
              </button>
              <button
                type="button"
                class="mini danger"
                :title="$t('chat.deleteChat')"
                :aria-label="$t('chat.deleteChatNamed', { name: chatTitle(c.title) })"
                @click="removeChat(c.id)"
              >
                <Trash2 :size="12" />
              </button>
            </li>
          </ul>
        </div>

        <div ref="scroller" class="stream" aria-live="polite">
          <!-- states in which one cannot chat: explained, with the remedy -->
          <div v-if="statusError" class="notice err"><CircleAlert :size="15" /> {{ statusError }}</div>
          <div v-else-if="status && !status.enabled" class="notice">
            <ShieldAlert :size="15" />
            <div><strong>{{ $t('chat.offTitle') }}</strong><p class="muted">{{ $t('chat.offBody') }}</p></div>
          </div>
          <div v-else-if="status && !status.models.length" class="notice">
            <ShieldAlert :size="15" />
            <div>
              <strong>{{ $t('chat.noModelsTitle') }}</strong>
              <p class="muted">{{ user?.is_superuser ? $t('chat.noModelsAdmin') : $t('chat.noModelsUser') }}</p>
              <NuxtLink v-if="user?.is_superuser" to="/admin" class="btn-link">{{ $t('chat.openAdmin') }}</NuxtLink>
            </div>
          </div>

          <div v-else-if="!turns.length" class="empty">
            <h2>{{ $t('chat.emptyTitle') }}</h2>
            <p class="muted">{{ metadataOnly ? $t('chat.metadataEmptyBody') : $t('chat.emptyBody') }}</p>
            <div class="examples">
              <button v-for="ex in examples" :key="ex" class="example" type="button" :disabled="!ready" @click="send(ex)">
                {{ ex }} <ChevronRight :size="13" />
              </button>
            </div>
          </div>

          <template v-for="(turn, ti) in turns" :key="ti">
            <div v-if="turn.role === 'user'" class="msg user">
              <AiSpeaker who="you" />
              <p>{{ turn.text }}</p>
            </div>
            <article v-else class="msg bot" :aria-label="$t('aiCompliance.assistantLabel')">
              <AiSpeaker who="ai" :model="turn.model" />
              <AgentSteps v-if="turn.steps.length" :steps="turn.steps" :ds-name="dsName">
                <template #evidence="{ step: s }">
                  <div v-if="s.table" class="evidence">
                    <div class="evidence-head">
                      <span>{{ s.table.datasource }}</span>
                      <span class="muted num">{{ $t(s.table.truncated ? 'chat.rowsMore' : 'chat.rows', { n: nf.format(s.table.row_count) }) }}</span>
                    </div>
                    <DataGrid :result="s.table" />
                    <MiniChart v-if="s.chart" :spec="s.chart" :rows="s.table.rows" :name="s.table.datasource" />
                    <!-- the file holds the WHOLE result, not the rows shown: the server runs the saved query
                         again -->
                    <p v-if="turn.seq !== undefined && chatId" class="grabs">
                      <button type="button" class="mini" :disabled="grabbing !== ''" @click="grab(turn, s, 'csv')">
                        <Download :size="12" /> CSV
                      </button>
                      <button type="button" class="mini" :disabled="grabbing !== ''" @click="grab(turn, s, 'xlsx')">
                        <Download :size="12" /> Excel
                      </button>
                      <span v-if="s.table.truncated" class="muted small">{{ $t('chat.grabsWhole') }}</span>
                    </p>
                  </div>
                </template>
              </AgentSteps>
              <MarkdownLite v-if="turn.text" :text="turn.text" />
              <AgentQuestionCard
                v-if="turn.questions?.length"
                :questions="turn.questions"
                :answers="turn.answers"
                :locked="questionsLocked(ti, turn)"
                :autofocus="ti === turns.length - 1"
                @send="(message, answers) => answerCard(turn, message, answers)"
              />
              <AgentPlanCard
                v-if="turn.plan"
                :plan="turn.plan"
                :superseded="planShownAt.get(turn.plan.id) !== ti"
                @update="onPlanUpdate"
                @change="onPlanChange"
              />
              <p v-if="turn.streaming" class="activity" aria-live="polite">
                <span class="beat" aria-hidden="true" />
                <Transition name="swap" mode="out-in">
                  <span :key="activityLabel(turn)">{{ activityLabel(turn) }}</span>
                </Transition>
              </p>
              <p v-if="turn.error" class="notice err inline"><CircleAlert :size="14" /> {{ turn.error }}</p>
              <!-- what THIS turn cost: the number comes from the model, not from a price list written by
                   us -->
              <p v-if="turn.usage && !turn.streaming" class="cost">
                <span :title="$t('chat.costHint')">{{ money(turn.usage.cost_usd) }}</span>
                <span class="muted">· {{ $t('chat.tokens', { i: nf.format(turn.usage.input_tokens), o: nf.format(turn.usage.output_tokens) }) }}</span>
              </p>
            </article>
          </template>
        </div>

        <form class="composer" @submit.prevent="send()">
          <div class="field">
            <!-- while waiting for the consent the assistant IS there: saying «not available» would be
                 false, and the reason is written in the dialog in front already -->
            <textarea
              ref="input"
              v-model="draft"
              rows="1"
              :placeholder="changeHint ? $t('chat.plan.changeHint') : ready || awaitingConsent ? $t('chat.placeholder') : $t('chat.placeholderOff')"
              :aria-label="$t('chat.placeholder')"
              :disabled="!ready"
              @keydown="onKey"
            />
            <button v-if="busy" class="send stop" type="button" :title="$t('chat.stop')" :aria-label="$t('chat.stop')" @click="stop"><Square :size="14" /></button>
            <button v-else class="primary send" type="submit" :disabled="!ready || !draft.trim()" :title="$t('chat.send')" :aria-label="$t('chat.send')"><Send :size="15" /></button>
          </div>
          <div class="under">
            <label class="pick">
              <span>{{ $t('chat.model') }}</span>
              <Select v-model="model" :options="modelOptions" :aria-label="$t('chat.model')" :disabled="!ready || busy" />
            </label>
            <label v-if="engineOptions.length" class="pick">
              <span>{{ $t('chat.engineLabel') }}</span>
              <Select v-model="engine" :options="engineOptions" :aria-label="$t('chat.engineLabel')" :disabled="!ready || busy" class="pick-engine" />
            </label>
            <span v-if="focus.length" class="muted">{{ $t('chat.focusCount', { n: focus.length }) }}</span>
          </div>
          <p v-if="metadataOnly" class="ai-notice mode"><ShieldCheck :size="12" aria-hidden="true" /> {{ $t('chat.metadataOnly') }}</p>
          <p class="ai-notice"><Sparkles :size="12" aria-hidden="true" /> {{ $t('aiCompliance.disclaimer') }}</p>
        </form>
      </section>
    </div>
  </AppShell>
</template>

<style scoped>
/* the page takes the whole area under the bar: cancels the shell's padding (22px 24px) */
.chat { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 0; height: calc(100vh - 52px); height: calc(100dvh - 52px); margin: -22px -24px; }

/* ── rail ─────────────────────────────────────────────────────────────── */
.rail { display: flex; flex-direction: column; min-height: 0; border-right: 1px solid var(--border); background: var(--panel); }
.rail[open] { overflow: hidden; }
.rail-head { display: flex; align-items: center; gap: 7px; padding: 14px 14px 6px; font-size: 12px; font-weight: 600; color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; list-style: none; cursor: default; }
.rail-head::-webkit-details-marker { display: none; }
.rail-lead { margin: 0; padding: 0 14px 10px; font-size: 12px; line-height: 1.4; }
.rail-search { display: flex; align-items: center; gap: 6px; margin: 0 12px 8px; padding: 0 8px; border: 1px solid var(--control-border); border-radius: 8px; background: var(--bg-soft); color: var(--muted); }
.rail-search input { flex: 1; min-width: 0; padding: 6px 0; border: 0; background: transparent; font-size: 13px; box-shadow: none; }
.rail-search:focus-within { border-color: var(--accent); }
.rail-list { flex: 1; min-height: 0; margin: 0; padding: 0 6px 12px; list-style: none; overflow-y: auto; }
.rail-empty { padding: 10px 8px; font-size: 12px; }
.ds { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; width: 100%; padding: 7px 8px; border: 1px solid transparent; border-radius: 8px; background: transparent; text-align: left; box-shadow: none; }
.ds:hover { background: var(--panel-2); box-shadow: none; }
.ds.on { background: var(--tint-accent, color-mix(in srgb, var(--accent) 12%, transparent)); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
.ds-name { display: inline-flex; align-items: center; gap: 5px; font-size: 13px; font-weight: 500; color: var(--text); overflow-wrap: anywhere; }
.ds-check { color: var(--accent); flex: none; }
.ds-about { display: block; width: 100%; font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ds-meta { display: flex; flex-wrap: wrap; gap: 3px 9px; font-size: 12px; color: var(--muted); }
.ds-ready { display: inline-flex; align-items: center; color: var(--accent-hi); flex: none; }
.ds-doc { display: inline-flex; align-items: center; gap: 3px; color: var(--accent-2); }
.num { font-variant-numeric: tabular-nums; }

/* ── conversation ──────────────────────────────────────────────────────── */
.talk { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.talk-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 24px 10px; }
.talk-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; font-size: 19px; font-weight: 700; }
.stream { flex: 1; min-height: 0; overflow-y: auto; padding: 6px 24px 18px; scroll-behavior: smooth; }
.stream > * { max-width: 780px; margin-inline: auto; }
.empty { padding-top: 8vh; }
.empty h2 { margin: 0 0 6px; font-size: 16px; font-weight: 700; }
.empty p { margin: 0 0 16px; max-width: 62ch; line-height: 1.5; }
.examples { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
.example { display: inline-flex; align-items: center; gap: 6px; padding: 7px 11px; font-size: 13px; text-align: left; }

.msg { margin-top: 16px; }
.msg.user { display: flex; flex-direction: column; align-items: flex-end; }
.msg.user p { margin: 0; max-width: 78%; padding: 8px 12px; border-radius: 12px 12px 3px 12px; background: var(--panel-2); border: 1px solid var(--border-soft); font-size: 13.5px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.evidence { margin: 6px 0 2px 19px; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--panel); }
.evidence-head { display: flex; justify-content: space-between; gap: 10px; padding: 6px 10px; font-size: 12px; font-weight: 500; border-bottom: 1px solid var(--border-soft); }
.evidence :deep(.datagrid) { max-height: 260px; overflow: auto; }

@media (prefers-reduced-motion: reduce) { .stream { scroll-behavior: auto; } }

.notice { display: flex; align-items: flex-start; gap: 9px; margin-top: 18px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); font-size: 13px; line-height: 1.45; }
.notice p { margin: 3px 0 8px; }
.notice svg { flex: none; margin-top: 2px; color: var(--muted); }
.notice.err { background: color-mix(in srgb, var(--danger) 10%, transparent); border-color: color-mix(in srgb, var(--danger) 40%, transparent); color: var(--text); }
.notice.err svg { color: var(--danger); }
.notice.inline { margin-top: 8px; }

/* ── composer ──────────────────────────────────────────────────────────── */
.composer { padding: 10px 24px 14px; border-top: 1px solid var(--border-soft); background: var(--bg); }
.composer > * { max-width: 780px; margin-inline: auto; }
.field { display: flex; align-items: flex-end; gap: 8px; padding: 6px 6px 6px 12px; border: 1px solid var(--control-border); border-radius: 12px; background: var(--bg-soft); }
.field:focus-within { border-color: var(--accent); }
.field textarea { flex: 1; min-width: 0; max-height: 160px; padding: 6px 0; border: 0; background: transparent; resize: none; font: inherit; font-size: 13.5px; line-height: 1.5; box-shadow: none; caret-color: var(--accent); }
.field textarea:focus { outline: none; box-shadow: none; }
.send { display: grid; place-items: center; flex: none; width: 34px; height: 34px; padding: 0; border-radius: 9px; }
.send.stop { background: var(--panel-2); color: var(--text); }
.under { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 14px; margin-top: 7px; font-size: 12px; }
.pick { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); }
.pick :deep(.select) { min-width: 210px; }
.pick .pick-engine:deep(.select), .pick-engine { min-width: 150px; }
.hint { margin-left: auto; }
/* the AI Act notice: always on screen under the box where the user writes */
.ai-notice { display: flex; align-items: center; justify-content: center; gap: 6px; margin: 8px 0 0; font-size: 12px; color: var(--muted); text-align: center; }
.ai-notice svg { flex: none; color: var(--accent-hi); }
/* metadata-only mode: two lines, under the box and as wide as it */
.ai-notice.mode { max-width: 780px; margin-inline: auto; justify-content: flex-start; align-items: flex-start; text-align: left; }
.ai-notice.mode svg { margin-top: 2px; }

@media (max-width: 900px) {
  .chat { grid-template-columns: 1fr; height: auto; min-height: calc(100dvh - 52px); }
  .rail { border-right: 0; border-bottom: 1px solid var(--border); }
  .rail:not([open]) .rail-list { display: none; }
  .rail-head { cursor: pointer; }
  .rail-list { max-height: 220px; }
  .stream { padding-inline: 16px; }
  .composer { padding-inline: 16px; position: sticky; bottom: 0; }
  .talk-head { padding-inline: 16px; }
  .hint { margin-left: 0; }
}
/* under 760px the shell narrows the padding to 16px 14px */
@media (max-width: 760px) { .chat { margin: -16px -14px; } }

/* The live turn's status line. The pulse is a loading indicator, of the same family as
   the skeletons' shimmer: it is not a second moment of authored motion. Under
   prefers-reduced-motion the still dot stays, and the sentence — which is the real
   information — changes all the same. */
.activity { display: flex; align-items: center; gap: 8px; margin: 8px 0 0; font-size: 12px; color: var(--muted); min-height: 18px; }
.beat {
  width: 7px; height: 7px; border-radius: 999px; background: var(--accent);
  box-shadow: 0 0 0 0 var(--accent); animation: beat 1.4s ease-out infinite; flex: none;
}
@keyframes beat {
  0%   { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 45%, transparent); opacity: 1; }
  70%  { box-shadow: 0 0 0 6px color-mix(in srgb, var(--accent) 0%, transparent); opacity: 0.55; }
  100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 0%, transparent); opacity: 1; }
}
.swap-enter-active, .swap-leave-active { transition: opacity 0.16s ease, transform 0.16s ease; }
.swap-enter-from { opacity: 0; transform: translateY(3px); }
.swap-leave-to { opacity: 0; transform: translateY(-3px); }
@media (prefers-reduced-motion: reduce) {
  .beat { animation: none; }
  .swap-enter-active, .swap-leave-active { transition: none; }
}

/* `.small` and `.mini` live in listpage.css, which this page does not import: without
   these lines the meta rendered bigger than the title above it and the bin was not red.
   Defined here, at the size the world prescribes. */
.nohistory {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--muted); padding: 3px 8px;
}
.chats .small, .chatsearch .count { font-size: 12px; }
.talk-head .mini, .chats .mini { padding: 3px 8px; min-height: 24px; }
.chats .mini.danger { border-color: var(--danger); color: var(--danger); }
.chats .mini.danger:hover:not(:disabled) { background: var(--danger-fill); color: var(--on-danger); }

/* search on the conversations: the same pill as the list pages */
.chatsearch {
  position: sticky; top: 0; z-index: 1; display: flex; align-items: center; gap: 6px;
  margin-bottom: 6px; padding: 5px 9px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
}
.chatsearch > svg { color: var(--muted); flex: none; }
.chatsearch input {
  flex: 1; min-width: 0; border: 0; background: none; color: var(--text); font: inherit; font-size: 12.5px; padding: 0;
}
.chatsearch input:focus { outline: none; box-shadow: none; }
.chatsearch:focus-within { border-color: var(--accent); }
.chatsearch input::-webkit-search-cancel-button { display: none; }
.chatsearch .count { flex: none; font-variant-numeric: tabular-nums; }

.grabs { display: flex; align-items: center; gap: 6px; margin: 8px 0 0; }

/* a turn's cost: present but quiet — it is information, not the content */
.cost { margin: 6px 0 0; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; display: flex; gap: 6px; }
.total { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; margin-left: auto; }
.chats { border-bottom: 1px solid var(--border-soft); padding: 8px 16px; max-height: 30vh; overflow-y: auto; }
.chats ul { list-style: none; margin: 0; padding: 0; }
.chats li { display: flex; align-items: center; gap: 6px; }
.chats li + li { margin-top: 2px; }
.chats .pick { flex: 1; display: flex; flex-direction: column; align-items: flex-start; gap: 1px; padding: 6px 8px; border: 0; background: none; color: var(--text); text-align: left; border-radius: 6px; cursor: pointer; }
.chats .pick:hover:not(:disabled) { background: var(--panel-2); box-shadow: none; }
.chats .pick:disabled { opacity: 0.5; cursor: not-allowed; }
.chats li.sel .pick { background: var(--tint-accent); box-shadow: inset 2px 0 0 var(--accent); }
.chats .t { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 46ch; }
</style>
