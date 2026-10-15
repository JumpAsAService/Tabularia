<script setup lang="ts">
// The flow editor's assistant. The user asks; the assistant changes the canvas with its
// tools and checks each step on a preview of the data; when the turn ends the new canvas
// comes back and the editor applies it as ONE Undo step. It never saves, runs or
// schedules the flow: that stays with the user.
//
// The canvas sent is compared with the canvas at the end of the turn: if the user
// changed it meanwhile, the assistant's version is not applied over their work without
// asking.
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertTriangle, Check, CircleAlert, LoaderCircle, Plus, Send, Sparkles, Square, Trash2, Undo2 } from 'lucide-vue-next'
import { useAi, type AiDone, type AiStatus, type AiTable, type AiUsage } from '~/composables/useAi'
import { useAiDisclosure } from '~/composables/useAiDisclosure'
import { useLocale } from '~/composables/useLocale'
import AiDisclosure from '~/components/ui/AiDisclosure.vue'
import DataGrid from '~/components/ui/DataGrid.vue'
import MarkdownLite from '~/components/ui/MarkdownLite.vue'
import Select from '~/components/ui/Select.vue'

const props = defineProps<{
  /** The tab is open: the disclosure and the conversation wait for it. */
  active: boolean
  status: AiStatus
  flowId: number | null
  engine: string | null
  selectedId: string | null
  /** The canvas as the editor saves it, now. */
  snapshot: () => string
  /** The editor's id counters. */
  counters: () => Record<string, number>
}>()
const emit = defineEmits<{ (e: 'apply', change: { definition: string; touched: string[] }): void }>()

const { t } = useI18n()
const { locale } = useLocale()
const ai = useAi()
const { accettata, leggi, accetta } = useAiDisclosure()

interface Step {
  id: string
  name: string
  args: Record<string, any>
  state: 'run' | 'ok' | 'warn' | 'error'
  node?: string
  error?: string
  table?: AiTable
  open?: boolean
}
interface Turn {
  question: string
  text: string
  steps: Step[]
  streaming: boolean
  error?: string
  usage?: AiUsage
  change?: { definition: string; touched: string[] }
  outcome?: 'applied' | 'pending' | 'discarded' | 'none'
}

const MODEL_KEY = 'tabularia.flowAssistant.model'
const model = ref('')
const turns = ref<Turn[]>([])
const chatId = ref<number | null>(null)
const draft = ref('')
const busy = ref(false)
const scroller = ref<HTMLElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
let controller: AbortController | null = null

const modelOptions = computed(() => props.status.models.map((m) => ({ value: m, label: m })))
const ready = computed(() => accettata.value === true && !!model.value)
const examples = computed(() => [t('flowAssistant.example1'), t('flowAssistant.example2'), t('flowAssistant.example3')])

function pickModel() {
  let saved = ''
  try {
    saved = localStorage.getItem(MODEL_KEY) ?? ''
  } catch {
    /* no storage: the default model */
  }
  model.value = props.status.models.includes(saved) ? saved : (props.status.default_model ?? props.status.models[0] ?? '')
}
watch(model, (m) => {
  try {
    if (m) localStorage.setItem(MODEL_KEY, m)
  } catch {
    /* remembering the choice is a convenience */
  }
})

function scrollDown() {
  nextTick(() => {
    const el = scroller.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// tool arguments come back from the saved history as text or as an object
function args(raw: unknown): Record<string, any> {
  if (raw && typeof raw === 'object') return raw as Record<string, any>
  try {
    return JSON.parse(String(raw ?? '{}')) ?? {}
  } catch {
    return {}
  }
}

async function loadLatest() {
  turns.value = []
  chatId.value = null
  if (props.flowId == null) return
  try {
    const d = await ai.latestFlowChat(props.flowId)
    if (!d) return
    chatId.value = d.id
    turns.value = d.messages.map((m) => ({
      question: m.question,
      text: m.answer,
      streaming: false,
      usage: m.usage,
      steps: m.steps.map((s, i) => ({
        id: s.id ?? `h${i}`, name: s.name, args: args(s.args), state: 'ok' as const, table: s.table, node: s.node,
      })),
    }))
    scrollDown()
  } catch {
    /* no earlier conversation to show: start fresh */
  }
}

// Nothing happens until the user opens the tab: an editor never asks someone who
// does not use the assistant to read the disclosure, nor loads a conversation for them.
let started = false
function start() {
  if (started || !props.active) return
  started = true
  leggi()
  pickModel()
  loadLatest()
}
onMounted(start)
watch(() => props.active, start)
// a different flow opened in the same editor; a new flow saved for the first time keeps its conversation
watch(() => props.flowId, (now, before) => {
  if (started && before != null && now !== before) loadLatest()
})

async function send(text?: string) {
  const message = (text ?? draft.value).trim()
  if (!message || busy.value || !ready.value) return
  const sent = props.snapshot()
  settlePending()
  const turn = reactive<Turn>({ question: message, text: '', steps: [], streaming: true })
  let paragraphBreak = false
  turns.value.push(turn)
  draft.value = ''
  busy.value = true
  controller = new AbortController()
  scrollDown()
  try {
    await ai.flowAssist(
      {
        message, model: model.value, chat_id: chatId.value, flow_id: props.flowId, definition: sent,
        engine: props.engine, locale: locale.value, selected: props.selectedId, counters: props.counters(),
      },
      {
        onText: (delta) => {
          // the model writes a little before a tool call and more after it: a paragraph each
          if (paragraphBreak && turn.text) turn.text += '\n\n'
          paragraphBreak = false
          turn.text += delta
          scrollDown()
        },
        onToolCall: (call) => {
          paragraphBreak = true
          turn.steps.push({ id: call.id, name: call.name, args: call.args ?? {}, state: 'run' })
          scrollDown()
        },
        onToolResult: (r) => {
          const step = turn.steps.find((s) => s.id === r.id)
          if (!step) return
          step.state = !r.ok ? 'error' : r.warning ? 'warn' : 'ok'
          step.error = r.error ?? r.warning
          step.node = r.node
          step.table = r.table
        },
        onDone: (done) => finish(turn, done, sent),
        onError: (msg, _code, extra) => {
          turn.error = msg
          // a turn stopped halfway still hands back what it built
          if (extra?.definition) offer(turn, { definition: extra.definition, touched: extra.touched ?? [] }, sent)
        },
      },
      controller.signal,
    )
  } catch (e: any) {
    turn.error = e?.name === 'AbortError' ? t('flowAssistant.stopped') : String(e?.message ?? e)
  } finally {
    turn.streaming = false
    busy.value = false
    controller = null
    for (const s of turn.steps) if (s.state === 'run') s.state = 'error' // interrupted
    scrollDown()
    nextTick(() => input.value?.focus())
  }
}

function finish(turn: Turn, done: AiDone, sent: string) {
  chatId.value = done.chat_id ?? chatId.value
  turn.usage = done.usage
  if (!done.definition) {
    turn.outcome = 'none'
    return
  }
  offer(turn, { definition: done.definition, touched: done.touched ?? [] }, sent)
}

function offer(turn: Turn, change: { definition: string; touched: string[] }, sent: string) {
  turn.change = change
  if (props.snapshot() === sent) {
    emit('apply', change)
    turn.outcome = 'applied'
  } else {
    turn.outcome = 'pending' // the user changed the canvas while the assistant worked
  }
}

// a version built on an older canvas is not offered once another turn has started
// or been applied: it would take back everything that came after it
function settlePending(except?: Turn) {
  for (const t of turns.value) if (t !== except && t.outcome === 'pending') t.outcome = 'discarded'
}

function applyAnyway(turn: Turn) {
  if (!turn.change) return
  settlePending(turn)
  emit('apply', turn.change)
  turn.outcome = 'applied'
}

async function removeConversation() {
  if (busy.value || chatId.value == null) return
  if (!confirm(t('flowAssistant.deleteConfirm'))) return
  try {
    await ai.removeChat(chatId.value)
  } catch {
    /* already gone: nothing to keep */
  }
  reset()
}

function stop() {
  controller?.abort()
}

function reset() {
  if (busy.value) return
  turns.value = []
  chatId.value = null
  nextTick(() => input.value?.focus())
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}

function stepLabel(s: Step): string {
  const a = s.args
  switch (s.name) {
    case 'get_flow':
      return t('flowAssistant.stepGetFlow')
    case 'list_datasources':
      return t('flowAssistant.stepListDatasources')
    case 'describe_datasource':
      return t('flowAssistant.stepDescribe', { id: a.datasource_id ?? '…' })
    case 'add_source':
      return t('flowAssistant.stepAddSource', { node: s.node ?? '…' })
    case 'add_step':
      return t('flowAssistant.stepAddStep', { op: a.operation ?? '…', node: s.node ?? '…' })
    case 'change_step':
      return t('flowAssistant.stepChangeStep', { node: a.node ?? '…' })
    case 'connect':
      return t('flowAssistant.stepConnect', { from: a.from_node ?? '…', to: a.to_node ?? '…' })
    case 'remove_node':
      return t('flowAssistant.stepRemove', { node: a.node ?? '…' })
    case 'list_folders':
      return t('flowAssistant.stepListFolders')
    case 'add_output':
      return t('flowAssistant.stepAddOutput', { name: a.datasource_name ?? '…' })
    case 'preview':
      return t('flowAssistant.stepPreview', { node: a.node ?? '…' })
    default:
      return s.name
  }
}
</script>

<template>
  <section class="fa" :aria-label="$t('flowAssistant.title')">
    <!-- the gate: the assistant is not used before the user has read who answers -->
    <AiDisclosure :open="active && accettata === false" @accept="accetta" />
    <header class="fa-head">
      <h3><Sparkles :size="15" /> {{ $t('flowAssistant.title') }}</h3>
      <span class="fa-head-actions">
        <button v-if="turns.length" class="fa-mini" type="button" :disabled="busy" @click="reset">
          <Plus :size="13" /> {{ $t('flowAssistant.newConversation') }}
        </button>
        <button
          v-if="chatId != null"
          class="fa-mini danger"
          type="button"
          :disabled="busy"
          :title="$t('flowAssistant.deleteConversation')"
          :aria-label="$t('flowAssistant.deleteConversation')"
          @click="removeConversation"
        ><Trash2 :size="13" /></button>
      </span>
    </header>

    <div ref="scroller" class="fa-stream" aria-live="polite">
      <div v-if="!turns.length" class="fa-empty">
        <p>{{ $t('flowAssistant.emptyBody') }}</p>
        <button
          v-for="ex in examples"
          :key="ex"
          type="button"
          class="fa-example"
          :disabled="!ready || busy"
          @click="send(ex)"
        >{{ ex }}</button>
      </div>

      <template v-for="(turn, ti) in turns" :key="ti">
        <p class="fa-q">{{ turn.question }}</p>
        <div class="fa-a">
          <ol v-if="turn.steps.length" class="fa-steps">
            <li v-for="s in turn.steps" :key="s.id" class="fa-step" :class="s.state">
              <span class="fa-step-line">
                <LoaderCircle v-if="s.state === 'run'" :size="12" class="spin fa-ico" />
                <AlertTriangle v-else-if="s.state === 'warn'" :size="12" class="fa-ico" />
                <CircleAlert v-else-if="s.state === 'error'" :size="12" class="fa-ico" />
                <Check v-else :size="12" class="fa-ico" />
                <span>{{ stepLabel(s) }}</span>
                <button
                  v-if="s.table"
                  class="fa-link"
                  type="button"
                  :aria-expanded="!!s.open"
                  @click="s.open = !s.open"
                >{{ s.open ? $t('flowAssistant.hideRows') : $t('flowAssistant.showRows', { n: s.table.row_count }) }}</button>
              </span>
              <span v-if="s.error" class="fa-step-err">{{ s.error }}</span>
              <div v-if="s.table && s.open" class="fa-table"><DataGrid :result="s.table" /></div>
            </li>
          </ol>
          <MarkdownLite v-if="turn.text" :text="turn.text" />
          <p v-if="turn.streaming" class="fa-activity"><LoaderCircle :size="12" class="spin" /> {{ $t('flowAssistant.working') }}</p>
          <p v-if="turn.error" class="fa-note err"><CircleAlert :size="13" /> <span>{{ turn.error }}</span></p>
          <p v-if="turn.outcome === 'applied'" class="fa-note ok"><Undo2 :size="13" /> <span>{{ $t('flowAssistant.applied') }}</span></p>
          <div v-else-if="turn.outcome === 'pending'" class="fa-note warn" role="alert">
            <AlertTriangle :size="13" />
            <span>{{ $t('flowAssistant.canvasChanged') }}</span>
            <span class="fa-note-actions">
              <button class="fa-mini primary" type="button" @click="applyAnyway(turn)">{{ $t('flowAssistant.applyAnyway') }}</button>
              <button class="fa-mini" type="button" @click="turn.outcome = 'discarded'">{{ $t('flowAssistant.discard') }}</button>
            </span>
          </div>
          <p v-else-if="turn.outcome === 'discarded'" class="fa-note"><span>{{ $t('flowAssistant.discarded') }}</span></p>
        </div>
      </template>
    </div>

    <form class="fa-composer" @submit.prevent="send()">
      <div class="fa-field">
        <textarea
          ref="input"
          v-model="draft"
          rows="2"
          :placeholder="selectedId ? $t('flowAssistant.placeholderSelected', { node: selectedId }) : $t('flowAssistant.placeholder')"
          :aria-label="$t('flowAssistant.inputLabel')"
          :disabled="!ready"
          @keydown="onKey"
        />
        <button
          v-if="busy"
          type="button"
          class="fa-send"
          :title="$t('flowAssistant.stop')"
          :aria-label="$t('flowAssistant.stop')"
          @click="stop"
        ><Square :size="13" /></button>
        <button
          v-else
          type="submit"
          class="fa-send primary"
          :disabled="!ready || !draft.trim()"
          :title="$t('flowAssistant.send')"
          :aria-label="$t('flowAssistant.send')"
        ><Send :size="13" /></button>
      </div>
      <div class="fa-under">
        <Select
          v-if="modelOptions.length > 1"
          v-model="model"
          class="fa-model"
          :options="modelOptions"
          :disabled="busy"
          :aria-label="$t('flowAssistant.model')"
        />
        <span class="muted">{{ $t('flowAssistant.hint') }}</span>
      </div>
    </form>
  </section>
</template>

<style scoped>
.fa { display: flex; flex-direction: column; min-height: 0; height: 100%; }
.fa-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px 6px; }
.fa-head h3 { display: inline-flex; align-items: center; gap: 6px; margin: 0; font-size: 13px; }
.fa-head-actions { display: inline-flex; gap: 4px; }
.fa-stream { flex: 1; min-height: 0; overflow-y: auto; padding: 4px 12px 12px; }
.fa-empty p { margin: 4px 0 10px; font-size: 12.5px; line-height: 1.5; color: var(--muted); }
.fa-example {
  display: block; width: 100%; margin-bottom: 6px; padding: 7px 10px; font-size: 12.5px; text-align: left;
  line-height: 1.4; border-radius: 8px;
}
.fa-q {
  margin: 14px 0 6px auto; width: fit-content; max-width: 92%; padding: 7px 10px;
  border-radius: 10px 10px 3px 10px; background: var(--panel-2); border: 1px solid var(--border-soft);
  font-size: 12.5px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere;
}
.fa-a { font-size: 12.5px; line-height: 1.5; overflow-wrap: anywhere; }
.fa-a :deep(p) { margin: 6px 0; }
.fa-steps { margin: 0 0 6px; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; }
.fa-step-line { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; font-size: 12px; color: var(--muted); }
.fa-ico { flex: none; }
.fa-step.ok .fa-ico { color: var(--accent-2); }
.fa-step.warn .fa-ico { color: var(--warning); }
.fa-step.error .fa-ico { color: var(--danger); }
.fa-step-err { display: block; margin: 2px 0 0 17px; font-size: 11.5px; color: var(--muted); }
.fa-link {
  padding: 0; border: 0; background: transparent; box-shadow: none; color: var(--accent-hi); font-size: 12px;
  text-decoration: underline; text-underline-offset: 2px;
}
.fa-link:hover { color: var(--text); box-shadow: none; }
.fa-table { margin: 4px 0 2px 17px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.fa-table :deep(.datagrid) { max-height: 220px; overflow: auto; }
.fa-activity { display: flex; align-items: center; gap: 6px; margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.fa-note {
  display: flex; align-items: flex-start; flex-wrap: wrap; gap: 6px; margin: 8px 0 0; padding: 7px 9px;
  border: 1px solid var(--border); border-radius: 8px; font-size: 12px; color: var(--muted);
}
.fa-note svg { flex: none; margin-top: 2px; }
.fa-note.ok { border-color: color-mix(in srgb, var(--accent-2) 40%, transparent); }
.fa-note.ok svg { color: var(--accent-2); }
.fa-note.warn { border-color: color-mix(in srgb, var(--warning) 45%, transparent); color: var(--text); }
.fa-note.warn svg { color: var(--warning); }
.fa-note.err { border-color: color-mix(in srgb, var(--danger) 45%, transparent); color: var(--text); }
.fa-note.err svg { color: var(--danger); }
.fa-note-actions { display: flex; gap: 6px; width: 100%; padding-left: 19px; }
.fa-mini { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; min-height: 24px; font-size: 12px; }
.fa-mini.danger:hover:not(:disabled) { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, transparent); }
.fa-composer { padding: 8px 12px 10px; border-top: 1px solid var(--border-soft); }
.fa-field {
  display: flex; align-items: flex-end; gap: 6px; padding: 4px 4px 4px 9px;
  border: 1px solid var(--control-border); border-radius: 10px; background: var(--bg-soft);
}
.fa-field:focus-within { border-color: var(--accent); }
.fa-field textarea {
  flex: 1; min-width: 0; max-height: 140px; padding: 5px 0; border: 0; background: transparent; resize: none;
  font: inherit; font-size: 12.5px; line-height: 1.45; box-shadow: none; caret-color: var(--accent);
}
.fa-field textarea:focus { outline: none; box-shadow: none; }
.fa-send { display: grid; place-items: center; flex: none; width: 30px; height: 30px; padding: 0; border-radius: 8px; }
.fa-under { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 10px; margin-top: 6px; font-size: 11.5px; }
.fa-model { min-width: 0; max-width: 100%; }
</style>
