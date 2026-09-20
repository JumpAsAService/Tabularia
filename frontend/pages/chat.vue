<script setup lang="ts">
// Assistente AI: una conversazione DENTRO il catalogo dell'utente. A sinistra le
// datasource che puo' leggere (le stesse che l'assistente puo' interrogare), al
// centro domande, passi dell'assistente e la tabella di ogni query: i numeri
// della risposta si controllano li' sotto.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  Sparkles, Send, Square, Plus, Database, Search, Check, FileText, ListTree, TableProperties,
  Code2, CircleAlert, ChevronRight, ShieldAlert, History, Trash2, Download,
} from 'lucide-vue-next'
import { errMessage, useApi } from '~/composables/useApi'
import MiniChart from '~/components/ui/MiniChart.vue'
import { useAi, type AiChartSpec, type AiChatSummary, type AiChatTotal, type AiStatus, type AiTable, type AiToolCall, type AiToolResult, type AiUsage } from '~/composables/useAi'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useToast } from '~/composables/useToast'
import { useProjects } from '~/composables/useProjects'
import { usePreferredEngine, type EngineOpt } from '~/composables/useEngine'
import { useLocale } from '~/composables/useLocale'
// import espliciti: nel container i componenti nuovi non vengono scansionati
import AppShell from '~/components/AppShell.vue'
import DataGrid from '~/components/ui/DataGrid.vue'
import MarkdownLite from '~/components/ui/MarkdownLite.vue'
import Select from '~/components/ui/Select.vue'

const { t } = useI18n()
const toast = useToast()
const { user } = useAuth()
const ai = useAi()
const dsApi = useDatasources()
const projectsApi = useProjects()
const api = useApi()
// il motore delle query si SCEGLIE, tra quelli disponibili e consentiti
// dall'amministratore: il preferito dell'utente e' solo il valore di partenza
const { defaultEngine } = usePreferredEngine()
const engines = ref<EngineOpt[]>([])
const engine = ref('')
const { locale } = useLocale()

type Step = { id: string; name: string; args: Record<string, any>; state: 'running' | 'ok' | 'error'; error?: string; count?: number; table?: AiTable; chart?: AiChartSpec }
type Turn =
  | { role: 'user'; text: string }
  | { role: 'assistant'; text: string; steps: Step[]; streaming: boolean; error?: string; open: Record<string, boolean>; usage?: AiUsage; phase?: Phase; phaseOn?: string; seq?: number }

// Cosa sta facendo l'assistente in questo istante. NON e' decorazione: ogni
// fase corrisponde a un evento vero dello stream, cosi' l'indicatore dice il
// vero come il resto della pagina («ogni passo e' ispezionabile»).
type Phase = 'thinking' | 'catalog' | 'fields' | 'query' | 'writing'
const PHASE_OF_TOOL: Record<string, Phase> = {
  list_datasources: 'catalog',
  describe_datasource: 'fields',
  query_datasource: 'query',
}

const status = ref<AiStatus | null>(null)
const statusError = ref('')
const model = ref('')
const datasources = ref<DatasourceInfo[]>([])
const folders = ref<Record<number, string>>({})
const filter = ref('')
const focus = ref<number[]>([])
const turns = ref<Turn[]>([])
// la storia sta sul SERVER: qui basta l'id della conversazione aperta
const chatId = ref<number | null>(null)
const chats = ref<AiChatSummary[]>([])
const chatsOpen = ref(false)
const chatFilter = ref('')
const chatTotal = ref<AiChatTotal | null>(null)
const draft = ref('')
const busy = ref(false)
let abort: AbortController | null = null
const scroller = ref<HTMLElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
const chatSearch = ref<HTMLInputElement | null>(null)
const nf = new Intl.NumberFormat()

// la ricerca guarda il titolo: e' l'unica cosa che l'elenco mostra, e cercare
// dentro conversazioni non caricate direbbe «trovato» senza saper mostrare dove
const chatsFiltered = computed(() => {
  const q = chatFilter.value.trim().toLowerCase()
  if (!q) return chats.value
  const parole = q.split(/\s+/)
  return chats.value.filter((c) => {
    const t = c.title.toLowerCase()
    return parole.every((w) => t.includes(w))
  })
})

const ready = computed(() => !!status.value?.enabled && !!status.value.models.length)
const modelOptions = computed(() => (status.value?.models ?? []).map((m) => ({ value: m, label: m })))
const engineOptions = computed(() => engines.value.filter((e) => e.available).map((e) => ({ value: e.id, label: e.label || e.id })))
const shown = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const list = datasources.value.filter((d) => d.key)
  return q ? list.filter((d) => d.name.toLowerCase().includes(q) || (d.description || '').toLowerCase().includes(q)) : list
})
// il conteggio lo fa il SERVER: la regola di «documentata» vive in un posto solo
const described = (d: DatasourceInfo) => d.described_columns
const dsName = (id: any) => datasources.value.find((d) => d.id === Number(id))?.name ?? `#${id}`
const examples = computed(() => [t('chat.example1'), t('chat.example2'), t('chat.example3')])

onMounted(async () => {
  try {
    status.value = await ai.status()
    model.value = status.value.default_model ?? status.value.models[0] ?? ''
  } catch (e) {
    statusError.value = errMessage(e)
  }
  try {
    // catalogo FRESCO (non quello in cache della sessione): la politica dei motori puo' essere cambiata
    engines.value = await api.engines()
    engine.value = defaultEngine(engines.value)
  } catch { /* senza catalogo decide il backend */ }
  try {
    const [list, projects] = await Promise.all([dsApi.list(), projectsApi.list()])
    datasources.value = list.sort((a, b) => a.name.localeCompare(b.name))
    folders.value = Object.fromEntries(projects.map((p: any) => [p.id, p.name]))
  } catch { /* il catalogo e' un aiuto: la chat funziona anche senza */ }
  await refreshChats()
  input.value?.focus()
})
onBeforeUnmount(() => abort?.abort())

/** L'etichetta della fase: una frase vera, non un rumore di fondo. */
function activityLabel(turn: Extract<Turn, { role: 'assistant' }>): string {
  const on = turn.phaseOn
  switch (turn.phase) {
    case 'catalog': return t('chat.actCatalog')
    case 'fields': return on ? t('chat.actFieldsOn', { name: on }) : t('chat.actFields')
    case 'query': return on ? t('chat.actQueryOn', { name: on }) : t('chat.actQuery')
    case 'writing': return t('chat.actWriting')
    default: return t('chat.actThinking')
  }
}

function toggleFocus(id: number) {
  focus.value = focus.value.includes(id) ? focus.value.filter((x) => x !== id) : [...focus.value, id].slice(-8)
}

function stepLabel(s: Step): string {
  if (s.name === 'list_datasources') return s.state === 'ok' ? t('chat.stepListDone', { n: s.count ?? 0 }) : t('chat.stepList')
  if (s.name === 'describe_datasource') return t('chat.stepDescribe', { name: dsName(s.args.datasource_id) })
  if (s.name === 'query_datasource') return t('chat.stepQuery', { name: dsName(s.args.datasource_id) })
  return s.name
}
const stepIcon = (s: Step) => (s.name === 'list_datasources' ? ListTree : s.name === 'describe_datasource' ? TableProperties : Code2)

async function scrollDown() {
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

async function send(text?: string) {
  const message = (text ?? draft.value).trim()
  if (!message || busy.value || !ready.value) return
  draft.value = ''
  turns.value.push({ role: 'user', text: message })
  const reply: Turn = { role: 'assistant', text: '', steps: [], streaming: true, open: {}, phase: 'thinking' }
  turns.value.push(reply)
  const live = turns.value[turns.value.length - 1] as Extract<Turn, { role: 'assistant' }>
  busy.value = true
  abort = new AbortController()
  scrollDown()
  try {
    await ai.chat(
      { message, model: model.value, chat_id: chatId.value, engine: engine.value || null, locale: locale.value, focus: focus.value },
      {
        onText: (delta) => { live.text += delta; live.phase = 'writing'; scrollDown() },
        onToolCall: (call: AiToolCall) => {
          // una frase, poi uno strumento, poi altro testo: il seguito e' un nuovo paragrafo
          if (live.text && !live.text.endsWith('\n\n')) live.text += '\n\n'
          live.steps.push({ id: call.id, name: call.name, args: call.args ?? {}, state: 'running' })
          live.phase = PHASE_OF_TOOL[call.name] ?? 'thinking'
          const su = dsName((call.args as any)?.datasource_id)
          live.phaseOn = su.startsWith('#') ? undefined : su
          scrollDown()
        },
        onToolResult: (res: AiToolResult) => {
          const step = live.steps.find((s) => s.id === res.id) ?? live.steps[live.steps.length - 1]
          if (!step) return
          step.state = res.ok ? 'ok' : 'error'
          step.error = res.error
          step.count = res.count
          step.table = res.table
          step.chart = res.chart
          live.phase = 'thinking'  // strumento finito: torna a ragionare
          scrollDown()
        },
        onDone: (done) => {
          chatId.value = done.chat_id
          chatTotal.value = done.chat_total
          live.usage = done.usage
          if (done.seq !== null) live.seq = done.seq
          void refreshChats()
        },
        onError: (msg) => { live.error = msg },
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
    input.value?.focus()
  }
}

function stop() { abort?.abort() }
function reset() {
  abort?.abort()
  turns.value = []
  chatId.value = null
  chatTotal.value = null
  input.value?.focus()
}

function toggleChats() {
  chatsOpen.value = !chatsOpen.value
  if (!chatsOpen.value) return
  // il titolo di una conversazione appena nata lo scrive il modello subito dopo
  // la risposta: rileggendo qui l'elenco e' sempre quello aggiornato
  void refreshChats()
  nextTick(() => chatSearch.value?.focus())
}

// ── conversazioni salvate ───────────────────────────────────────────────────
async function refreshChats() {
  try { chats.value = await ai.chats() } catch { /* l'elenco non e' essenziale */ }
}

/** Riapre una conversazione: le domande gia' fatte non si ripagano. */
async function openChat(id: number) {
  if (busy.value) return
  try {
    const d = await ai.openChat(id)
    turns.value = d.messages.flatMap((t) => ([
      { role: 'user', text: t.question } as Turn,
      {
        role: 'assistant', text: t.answer, streaming: false, open: {}, usage: t.usage, seq: t.seq,
        steps: t.steps.map((p, i) => ({
          id: p.id ?? `${t.seq}-${i}`, name: p.name, args: p.args ?? {}, state: 'ok' as const,
          table: p.table, chart: p.chart,
        })),
      } as Turn,
    ]))
    chatId.value = d.id
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

/** Scarica il risultato COMPLETO di un passo: la chat ne mostra al massimo 200
 *  righe, il file le contiene tutte perché il server riesegue la query salvata. */
async function grab(turn: Extract<Turn, { role: 'assistant' }>, step: Step, fmt: 'csv' | 'xlsx') {
  if (!chatId.value || turn.seq === undefined || grabbing.value) return
  grabbing.value = `${step.id}:${fmt}`
  try {
    const blob = await ai.exportStep(chatId.value, turn.seq, step.id, fmt)
    const nome = `${step.table?.datasource || 'risultato'}.${fmt}`.toLowerCase().replace(/[^a-z0-9._-]+/g, '_')
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = nome
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: any) {
    toast.error(errMessage(e))
  } finally {
    grabbing.value = ''
  }
}

/** La conversazione come Markdown: domande, risposte, e per ogni passo la query
 *  che l'ha prodotto. È il formato che si rilegge fra sei mesi e si incolla in
 *  un ticket, non uno screenshot. */
function downloadChat() {
  const righe: string[] = [`# ${chats.value.find((c) => c.id === chatId.value)?.title || t('chat.pageTitle')}`, '']
  for (const turn of turns.value) {
    if (turn.role === 'user') { righe.push(`## ${turn.text}`, ''); continue }
    for (const s of turn.steps) {
      const sql = (s.args as any)?.sql
      if (sql) righe.push('```sql', String(sql), '```', '')
      if (s.table) {
        const cols = s.table.columns.map((c) => c.name)
        righe.push(`| ${cols.join(' | ')} |`, `|${cols.map(() => '---').join('|')}|`)
        for (const r of s.table.rows) righe.push(`| ${cols.map((c) => String((r as any)[c] ?? '')).join(' | ')} |`)
        righe.push('')
      }
    }
    if (turn.text) righe.push(turn.text, '')
    if (turn.usage) righe.push(`*${money(turn.usage.cost_usd)} · ${t('chat.tokens', { i: nf.format(turn.usage.input_tokens), o: nf.format(turn.usage.output_tokens) })}*`, '')
  }
  const blob = new Blob([righe.join('\n')], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(chats.value.find((c) => c.id === chatId.value)?.title || 'conversazione').replace(/[^a-z0-9._-]+/gi, '_')}.md`.toLowerCase()
  a.click()
  URL.revokeObjectURL(url)
}

/** Il costo come lo riporta il modello, scritto nella lingua dell'utente.
 *  `null` = non determinabile, che non e' zero: meglio un trattino che un falso
 *  «gratis». Sotto il microdollaro si dice «meno di», perche' `2e-7` in
 *  un'interfaccia non e' un numero, e' un'uscita del formattatore. */
const MIN_COST = 0.000001
function money(v: string | null | undefined): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!isFinite(n)) return '—'
  const fmtUsd = (x: number, cifre: number) =>
    new Intl.NumberFormat(locale.value, {
      style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: cifre,
    }).format(x)
  if (n === 0) return fmtUsd(0, 2)
  if (n < MIN_COST) return t('chat.costUnder', { v: fmtUsd(MIN_COST, 6) })
  return fmtUsd(n, n < 0.01 ? 6 : 2)
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send() }
}
// il campo cresce col testo, fino a un tetto
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
    <div class="chat">
      <!-- ── catalogo: cio' che l'assistente puo' leggere ─────────────────── -->
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
                <!-- documentata per intero: la stessa cosa che il catalogo
                     marca «pronta per l'AI», detta qui in forma compatta -->
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

      <!-- ── conversazione ───────────────────────────────────────────────── -->
      <section class="talk">
        <header class="talk-head">
          <h1><Sparkles :size="18" /> {{ $t('chat.pageTitle') }}</h1>
          <span v-if="chatTotal && chatTotal.turns" class="total" :title="$t('chat.costHint')">
            {{ $t('chat.chatTotal', { c: money(chatTotal.cost_usd) }) }}
          </span>
          <button class="mini" type="button" :aria-expanded="chatsOpen" @click="toggleChats">
            <History :size="13" /> {{ $t('chat.history', { n: chats.length }) }}
          </button>
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
                <span class="t">{{ c.title }}</span>
                <span class="muted small">{{ $t('chat.turns', { n: c.turns }) }} · {{ money(c.cost_usd) }}</span>
              </button>
              <button
                type="button"
                class="mini danger"
                :title="$t('chat.deleteChat')"
                :aria-label="$t('chat.deleteChatNamed', { name: c.title })"
                @click="removeChat(c.id)"
              >
                <Trash2 :size="12" />
              </button>
            </li>
          </ul>
        </div>

        <div ref="scroller" class="stream" aria-live="polite">
          <!-- stati in cui non si puo' chattare: spiegati, con il rimedio -->
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
            <p class="muted">{{ $t('chat.emptyBody') }}</p>
            <div class="examples">
              <button v-for="ex in examples" :key="ex" class="example" type="button" :disabled="!ready" @click="send(ex)">
                {{ ex }} <ChevronRight :size="13" />
              </button>
            </div>
          </div>

          <template v-for="(turn, ti) in turns" :key="ti">
            <div v-if="turn.role === 'user'" class="msg user"><p>{{ turn.text }}</p></div>
            <article v-else class="msg bot">
              <ol v-if="turn.steps.length" class="steps">
                <li v-for="s in turn.steps" :key="s.id" class="step" :class="s.state">
                  <div class="step-line">
                    <component :is="stepIcon(s)" :size="13" class="step-ico" />
                    <span>{{ stepLabel(s) }}</span>
                    <button v-if="s.name === 'query_datasource' && s.args.sql" class="linkish" type="button" :aria-expanded="!!turn.open[s.id]" @click="turn.open[s.id] = !turn.open[s.id]">
                      {{ turn.open[s.id] ? $t('chat.hideSql') : $t('chat.showSql') }}
                    </button>
                  </div>
                  <pre v-if="turn.open[s.id] && s.args.sql" class="sql"><code>{{ s.args.sql }}</code></pre>
                  <p v-if="s.state === 'error' && s.error" class="step-err">{{ s.error }}</p>
                  <div v-if="s.table" class="evidence">
                    <div class="evidence-head">
                      <span>{{ s.table.datasource }}</span>
                      <span class="muted num">{{ $t(s.table.truncated ? 'chat.rowsMore' : 'chat.rows', { n: nf.format(s.table.row_count) }) }}</span>
                    </div>
                    <DataGrid :result="s.table" />
                    <MiniChart v-if="s.chart" :spec="s.chart" :rows="s.table.rows" :name="s.table.datasource" />
                    <!-- il file contiene il risultato INTERO, non le righe mostrate:
                         il server riesegue la query salvata -->
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
                </li>
              </ol>
              <MarkdownLite v-if="turn.text" :text="turn.text" />
              <p v-if="turn.streaming" class="activity" aria-live="polite">
                <span class="beat" aria-hidden="true" />
                <Transition name="swap" mode="out-in">
                  <span :key="activityLabel(turn)">{{ activityLabel(turn) }}</span>
                </Transition>
              </p>
              <p v-if="turn.error" class="notice err inline"><CircleAlert :size="14" /> {{ turn.error }}</p>
              <!-- quanto e' costato QUESTO turno: il numero viene dal modello,
                   non da un listino scritto da noi -->
              <p v-if="turn.usage && !turn.streaming" class="cost">
                <span :title="$t('chat.costHint')">{{ money(turn.usage.cost_usd) }}</span>
                <span class="muted">· {{ $t('chat.tokens', { i: nf.format(turn.usage.input_tokens), o: nf.format(turn.usage.output_tokens) }) }}</span>
              </p>
            </article>
          </template>
        </div>

        <form class="composer" @submit.prevent="send()">
          <div class="field">
            <textarea
              ref="input"
              v-model="draft"
              rows="1"
              :placeholder="ready ? $t('chat.placeholder') : $t('chat.placeholderOff')"
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
            <span class="muted hint">{{ $t('chat.disclaimer') }}</span>
          </div>
        </form>
      </section>
    </div>
  </AppShell>
</template>

<style scoped>
/* la pagina occupa tutta l'area sotto la barra: annulla il padding della shell (22px 24px) */
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

/* ── conversazione ────────────────────────────────────────────────────── */
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
.msg.user { display: flex; justify-content: flex-end; }
.msg.user p { margin: 0; max-width: 78%; padding: 8px 12px; border-radius: 12px 12px 3px 12px; background: var(--panel-2); border: 1px solid var(--border-soft); font-size: 13.5px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.steps { margin: 0 0 10px; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.step-line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 12px; color: var(--muted); }
.step.ok .step-ico { color: var(--accent-2); }
.step.error .step-ico { color: var(--danger); }
.step-err { margin: 3px 0 0 19px; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.linkish { padding: 0; border: 0; background: transparent; color: var(--accent-hi); font-size: 12px; box-shadow: none; text-decoration: underline; text-underline-offset: 2px; }
.linkish:hover { color: var(--text); box-shadow: none; }
.sql { margin: 5px 0 0 19px; padding: 8px 10px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); font-size: 12px; overflow-x: auto; }
.sql code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
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

/* ── compositore ──────────────────────────────────────────────────────── */
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
/* sotto i 760px la shell stringe il padding a 16px 14px */
@media (max-width: 760px) { .chat { margin: -16px -14px; } }

/* Riga di stato del turno vivo. La pulsazione e' un indicatore di caricamento,
   della stessa famiglia dello shimmer degli scheletri: non e' un secondo momento
   di movimento d'autore. Sotto prefers-reduced-motion resta il punto fermo, e
   la frase — che e' l'informazione vera — cambia lo stesso. */
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

/* `.small` e `.mini` vivono in listpage.css, che questa pagina non importa:
   senza queste righe la meta rendeva piu' grande del titolo che le sta sopra e
   il cestino non era rosso. Definite qui, alla misura che il mondo prescrive. */
.chats .small, .chatsearch .count { font-size: 12px; }
.talk-head .mini, .chats .mini { padding: 3px 8px; min-height: 24px; }
.chats .mini.danger { border-color: var(--danger); color: var(--danger); }
.chats .mini.danger:hover:not(:disabled) { background: var(--danger); color: #fff; }

/* ricerca sulle conversazioni: la stessa pillola delle pagine a elenco */
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

/* costo di un turno: presente ma sottovoce — e' un'informazione, non il contenuto */
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
