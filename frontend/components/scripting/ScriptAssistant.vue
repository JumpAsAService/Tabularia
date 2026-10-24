<script setup lang="ts">
// The script editor's assistant, beside the code. The user asks; the assistant answers
// and may propose files (whole files): the proposal is applied only when the user
// says so, as one change of the editor (Ctrl+Z takes it back). It never saves or runs.
//
// What goes to the model: the code, the environment, the names of the variables and of
// the inputs' columns, and the error of the last run when it failed — never a row of
// data (the user's choice of 2026-10-24); the gateway cuts the log down to the error.
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, CircleAlert, FileDiff, LoaderCircle, Plus, Send, Sparkles, Square, Wrench } from 'lucide-vue-next'
import { useAi, type AiDone, type AiStatus, type AiUsage, type ScriptProposal } from '~/composables/useAi'
import { useAiDisclosure } from '~/composables/useAiDisclosure'
import { useLocale } from '~/composables/useLocale'
import type { ScriptNodeRun } from '~/composables/useScripting'
import AiDisclosure from '~/components/ui/AiDisclosure.vue'
import MarkdownLite from '~/components/ui/MarkdownLite.vue'
import Select from '~/components/ui/Select.vue'
import AiSpeaker from '~/components/ui/AiSpeaker.vue'

interface ScriptFile { path: string; content: string }
export interface ScriptInputColumns { variable: string; columns: { name: string; dtype: string }[] }

const props = defineProps<{
  status: AiStatus
  environmentId: number
  files: ScriptFile[]
  entrypoint: string
  variables: string[]
  inputs: ScriptInputColumns[]
  output: 'required' | 'optional' | 'none'
  saved: boolean
  lastRun: ScriptNodeRun | null
  readOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'apply', proposal: ScriptProposal): void }>()

const { t } = useI18n()
const { locale } = useLocale()
const ai = useAi()
const { accettata, leggi, accetta } = useAiDisclosure()

interface Turn {
  question: string
  model: string
  text: string
  streaming: boolean
  error?: string
  usage?: AiUsage
  proposal?: ScriptProposal | null
  outcome?: 'pending' | 'applied' | 'discarded'
}

const MODEL_KEY = 'tabularia.scriptAssistant.model'
const model = ref('')
const turns = ref<Turn[]>([])
const chatId = ref<number | null>(null)
const draft = ref('')
const busy = ref(false)
const scroller = ref<HTMLElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
let controller: AbortController | null = null

const modelOptions = computed(() => props.status.models.map((m) => ({ value: m, label: m })))
const ready = computed(() => accettata.value === true && !!model.value && !props.readOnly)
const failed = computed(() => props.lastRun?.status === 'FAILURE' && !props.lastRun.stale)

onMounted(() => {
  leggi()
  let saved = ''
  try { saved = localStorage.getItem(MODEL_KEY) ?? '' } catch { /* no storage: the default model */ }
  model.value = props.status.models.includes(saved) ? saved : (props.status.default_model ?? props.status.models[0] ?? '')
})
watch(model, (m) => { try { if (m) localStorage.setItem(MODEL_KEY, m) } catch { /* a convenience */ } })

function scrollDown() {
  nextTick(() => { if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight })
}

// what a proposal changes, in lines, against the files now
function stat(p: ScriptProposal, path: string): string {
  const now = props.files.find((f) => f.path === path)?.content
  const next = p.files.find((f) => f.path === path)?.content ?? ''
  if (now == null) return t('scriptAssistant.newFile', { n: next.split('\n').length })
  const a = new Set(now.split('\n'))
  const b = new Set(next.split('\n'))
  const added = [...b].filter((l) => !a.has(l)).length
  const removed = [...a].filter((l) => !b.has(l)).length
  return `+${added} −${removed}`
}

async function send(text?: string, withError = false) {
  const message = (text ?? draft.value).trim()
  if (!message || busy.value || !ready.value) return
  for (const x of turns.value) if (x.outcome === 'pending') x.outcome = 'discarded' // built on older code
  const turn = reactive<Turn>({ question: message, model: model.value, text: '', streaming: true })
  turns.value.push(turn)
  draft.value = ''
  busy.value = true
  controller = new AbortController()
  scrollDown()
  const run = props.lastRun
  try {
    await ai.scriptAssist(
      {
        message, model: model.value, chat_id: chatId.value, environment_id: props.environmentId,
        files: props.files, entrypoint: props.entrypoint, variables: props.variables, inputs: props.inputs,
        output: props.output, saved: props.saved, locale: locale.value,
        // the gateway keeps only the error of this log for the model
        ...(withError || failed.value ? { run_log: run?.log ?? '', run_exit_code: run?.exit_code ?? null } : {}),
      },
      {
        onText: (d) => { turn.text += d; scrollDown() },
        onToolCall: () => {},
        onToolResult: () => {},
        onDone: (done: AiDone) => {
          chatId.value = done.chat_id ?? chatId.value
          turn.usage = done.usage
          if (done.proposal) {
            turn.proposal = done.proposal
            turn.outcome = 'pending'
          }
        },
        onError: (msg) => { turn.error = msg },
      },
      controller.signal,
    )
  } catch (e: any) {
    turn.error = e?.name === 'AbortError' ? t('scriptAssistant.stopped') : String(e?.message ?? e)
  } finally {
    turn.streaming = false
    busy.value = false
    controller = null
    scrollDown()
    nextTick(() => input.value?.focus())
  }
}

function apply(turn: Turn) {
  if (!turn.proposal) return
  emit('apply', turn.proposal)
  turn.outcome = 'applied'
}
function reset() {
  if (busy.value) return
  turns.value = []
  chatId.value = null
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}
</script>

<template>
  <section class="sa" :aria-label="$t('scriptAssistant.title')">
    <AiDisclosure :open="accettata === false" @accept="accetta" />
    <header class="sa-head">
      <h3><Sparkles :size="14" /> {{ $t('scriptAssistant.title') }}</h3>
      <button v-if="turns.length" class="sa-mini" type="button" :disabled="busy" :title="$t('scriptAssistant.newConversation')"
              :aria-label="$t('scriptAssistant.newConversation')" @click="reset"><Plus :size="13" /></button>
    </header>

    <div ref="scroller" class="sa-stream" aria-live="polite">
      <div v-if="!turns.length" class="sa-empty">
        <p>{{ $t('scriptAssistant.emptyBody') }}</p>
        <button v-if="failed" type="button" class="sa-fix" :disabled="!ready || busy" @click="send($t('scriptAssistant.fixPrompt'), true)">
          <Wrench :size="13" /> {{ $t('scriptAssistant.fixLastError') }}
        </button>
        <button type="button" class="sa-example" :disabled="!ready || busy" @click="send($t('scriptAssistant.example1'))">{{ $t('scriptAssistant.example1') }}</button>
        <button type="button" class="sa-example" :disabled="!ready || busy" @click="send($t('scriptAssistant.example2'))">{{ $t('scriptAssistant.example2') }}</button>
      </div>

      <template v-for="(turn, ti) in turns" :key="ti">
        <div class="sa-you">
          <AiSpeaker who="you" />
          <p class="sa-q">{{ turn.question }}</p>
        </div>
        <div class="sa-a" role="group" :aria-label="$t('aiCompliance.assistantLabel')">
          <AiSpeaker who="ai" :model="turn.model" />
          <MarkdownLite v-if="turn.text" :text="turn.text" />
          <p v-if="turn.streaming" class="sa-activity"><LoaderCircle :size="12" class="spin" /> {{ $t('scriptAssistant.working') }}</p>
          <p v-if="turn.error" class="sa-note err"><CircleAlert :size="13" /> <span>{{ turn.error }}</span></p>
          <div v-if="turn.proposal" class="sa-proposal" :class="turn.outcome">
            <div class="sa-ptitle"><FileDiff :size="13" /> {{ $t('scriptAssistant.proposal') }}</div>
            <ul>
              <li v-for="p in turn.proposal.changed" :key="p"><code>{{ p }}</code> <span class="muted">{{ stat(turn.proposal, p) }}</span></li>
              <li v-for="p in turn.proposal.removed" :key="`r${p}`"><code>{{ p }}</code> <span class="muted">{{ $t('scriptAssistant.removed') }}</span></li>
              <li v-if="turn.proposal.entrypoint !== entrypoint"><span class="muted">{{ $t('scriptAssistant.runs', { file: turn.proposal.entrypoint }) }}</span></li>
            </ul>
            <div v-if="turn.outcome === 'pending'" class="sa-pactions">
              <button type="button" class="primary sa-mini-btn" @click="apply(turn)"><Check :size="13" /> {{ $t('scriptAssistant.apply') }}</button>
              <button type="button" class="sa-mini-btn" @click="turn.outcome = 'discarded'">{{ $t('scriptAssistant.discard') }}</button>
            </div>
            <p v-else-if="turn.outcome === 'applied'" class="sa-note ok">{{ $t('scriptAssistant.applied') }}</p>
            <p v-else class="sa-note">{{ $t('scriptAssistant.discarded') }}</p>
          </div>
        </div>
      </template>
    </div>

    <form class="sa-composer" @submit.prevent="send()">
      <div class="sa-field">
        <textarea ref="input" v-model="draft" rows="2" :placeholder="$t('scriptAssistant.placeholder')"
                  :aria-label="$t('scriptAssistant.inputLabel')" :disabled="!ready" @keydown="onKey" />
        <button v-if="busy" type="button" class="sa-send" :title="$t('scriptAssistant.stop')" :aria-label="$t('scriptAssistant.stop')"
                @click="controller?.abort()"><Square :size="13" /></button>
        <button v-else type="submit" class="sa-send primary" :disabled="!ready || !draft.trim()" :title="$t('scriptAssistant.send')"
                :aria-label="$t('scriptAssistant.send')"><Send :size="13" /></button>
      </div>
      <p class="sa-notice"><Sparkles :size="12" aria-hidden="true" /> {{ $t('scriptAssistant.noData') }}</p>
      <p class="sa-notice">{{ $t('aiCompliance.disclaimer') }}</p>
      <Select v-if="modelOptions.length > 1" v-model="model" class="sa-model" :options="modelOptions" :disabled="busy"
              :aria-label="$t('scriptAssistant.model')" />
    </form>
  </section>
</template>

<style scoped>
.sa { display: flex; flex-direction: column; min-height: 0; height: 100%; }
.sa-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 2px 0 6px; }
.sa-head h3 { display: inline-flex; align-items: center; gap: 6px; margin: 0; font-size: 12.5px; }
.sa-mini { padding: 2px 6px; }
.sa-stream { flex: 1; min-height: 0; overflow-y: auto; padding-right: 2px; }
.sa-empty p { margin: 2px 0 10px; font-size: 12.5px; line-height: 1.5; color: var(--muted); }
.sa-example, .sa-fix { display: flex; align-items: center; gap: 6px; width: 100%; margin-bottom: 6px; padding: 7px 10px; font-size: 12.5px; text-align: left; line-height: 1.4; border-radius: 8px; }
.sa-fix { border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); }
.sa-you { display: flex; flex-direction: column; align-items: flex-end; margin-top: 12px; }
.sa-q { margin: 0 0 6px; width: fit-content; max-width: 92%; padding: 7px 10px; border-radius: 10px 10px 3px 10px;
  background: var(--panel-2); border: 1px solid var(--border-soft); font-size: 12.5px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
.sa-a { margin-top: 6px; font-size: 12.5px; line-height: 1.5; overflow-wrap: anywhere; }
.sa-a :deep(p) { margin: 6px 0; }
.sa-a :deep(pre) { max-height: 260px; overflow: auto; font-size: 11.5px; }
.sa-activity { display: flex; align-items: center; gap: 6px; margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.sa-note { display: flex; align-items: flex-start; gap: 6px; margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.sa-note.err { color: var(--danger); }
.sa-note.ok { color: var(--accent-2); }
.sa-proposal { margin-top: 8px; padding: 8px 10px; border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border)); border-radius: 9px;
  background: color-mix(in srgb, var(--accent) 6%, transparent); }
.sa-proposal.discarded { opacity: 0.6; }
.sa-ptitle { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }
.sa-proposal ul { margin: 6px 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
.sa-proposal code { font-size: 11.5px; }
.sa-pactions { display: flex; gap: 6px; }
.sa-mini-btn { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; }
.sa-composer { padding-top: 8px; border-top: 1px solid var(--border-soft); display: flex; flex-direction: column; gap: 4px; }
.sa-field { display: flex; gap: 6px; align-items: flex-end; }
.sa-field textarea { flex: 1; min-width: 0; resize: vertical; font-size: 12.5px; }
.sa-send { padding: 6px 9px; }
.sa-notice { display: flex; align-items: flex-start; gap: 5px; margin: 0; font-size: 11px; line-height: 1.4; color: var(--muted); }
.sa-notice svg { flex: none; margin-top: 2px; color: var(--accent-hi); }
.sa-model { margin-top: 2px; }
</style>
