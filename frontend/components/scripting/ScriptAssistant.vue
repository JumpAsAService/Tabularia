<script setup lang="ts">
// The script editor's assistant, beside the code. The user asks; the assistant answers
// and may propose files (whole files): the proposal is applied only when the user
// says so, as one change of the editor. It never saves or runs.
//
// Before that, the card shows what applying would do to the files AS THEY ARE NOW: a true
// difference per file, line by line, and a warning when a file it touches was edited after the
// proposal was written (applying would replace those edits, and the difference shows them
// going). After, «Undo» puts back every file the proposal touched, the removed ones too, for as
// long as nobody has edited them since.
//
// What goes to the model: the code, the environment, the names of the variables and of
// the inputs' columns, and the error of the last run when it failed — never a row of
// data (the user's choice of 2026-10-24); the gateway cuts the log down to the error.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, ChevronRight, CircleAlert, FileDiff, LoaderCircle, Plus, Send, Sparkles, Square, TriangleAlert, Undo2, Wrench } from 'lucide-vue-next'
import { aiModelLabel, aiModelOptions, aiProviderOf, aiProviders, useAi, type AiDone, type AiStatus, type AiUsage, type ScriptProposal } from '~/composables/useAi'
import { useAiDisclosure } from '~/composables/useAiDisclosure'
import { useLocale } from '~/composables/useLocale'
import { diffHunks, lineDiff, type DiffLine, type LineDiff } from '~/composables/lineDiff'
import type { ScriptNodeRun } from '~/composables/useScripting'
import AiDisclosure from '~/components/ui/AiDisclosure.vue'
import MarkdownLite from '~/components/ui/MarkdownLite.vue'
import Select from '~/components/ui/Select.vue'
import AiSpeaker from '~/components/ui/AiSpeaker.vue'
import ExpandBody from '~/components/ui/ExpandBody.vue'

interface ScriptFile { path: string; content: string }
/** A file given its content, or taken away (content null). */
export interface ScriptFileChange { path: string; content: string | null }
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
const emit = defineEmits<{
  (e: 'apply', proposal: ScriptProposal): void
  (e: 'revert', before: { files: ScriptFileChange[]; entrypoint: string }): void
}>()

const { t } = useI18n()
const { locale } = useLocale()
const ai = useAi()
const { accepted, read, accept } = useAiDisclosure()

interface Turn {
  question: string
  model: string
  text: string
  streaming: boolean
  error?: string
  usage?: AiUsage
  proposal?: ScriptProposal | null
  outcome?: 'pending' | 'applied' | 'discarded'
  /** the files as they were sent with the question: a proposal is written against these */
  sent?: Record<string, string>
  /** the files whose difference is open, by path */
  open?: Record<string, boolean>
  /** once applied: what each file it touched was before, what each line of the card said, and the entrypoint */
  before?: { files: ScriptFileChange[]; entrypoint: string }
  stats?: Record<string, string>
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

const modelOptions = computed(() => aiModelOptions(props.status))
const sentTo = computed(() => aiProviderOf(props.status, model.value))
const ready = computed(() => accepted.value === true && !!model.value && !props.readOnly)
const failed = computed(() => props.lastRun?.status === 'FAILURE' && !props.lastRun.stale)
const metadataOnly = computed(() => !!props.status.metadata_only)

onMounted(() => {
  read(metadataOnly.value, aiProviders(props.status))
  let saved = ''
  try { saved = localStorage.getItem(MODEL_KEY) ?? '' } catch { /* no storage: the default model */ }
  model.value = props.status.models.includes(saved) ? saved : (props.status.default_model ?? props.status.models[0] ?? '')
})
// the editor closed while the model writes: the answer is stopped, not paid for to no one
onBeforeUnmount(() => controller?.abort())
watch(model, (m) => { try { if (m) localStorage.setItem(MODEL_KEY, m) } catch { /* a convenience */ } })

function scrollDown() {
  nextTick(() => { if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight })
}

const contentNow = (path: string): string | null => props.files.find((f) => f.path === path)?.content ?? null

// What a proposal would change in a file, against the file as it is now. Kept until the file or the
// proposal changes: the card reads it for the count and for every line it draws.
const diffs = new WeakMap<ScriptProposal, Map<string, { now: string; diff: LineDiff }>>()
function diffOf(p: ScriptProposal, path: string): LineDiff {
  const now = contentNow(path) ?? ''
  let byPath = diffs.get(p)
  if (!byPath) diffs.set(p, (byPath = new Map()))
  const kept = byPath.get(path)
  if (kept && kept.now === now) return kept.diff
  const diff = lineDiff(now, p.files.find((f) => f.path === path)?.content ?? '')
  byPath.set(path, { now, diff })
  return diff
}
function stat(p: ScriptProposal, path: string): string {
  if (contentNow(path) === null) return t('scriptAssistant.newFile', { n: diffOf(p, path).added })
  const d = diffOf(p, path)
  return `+${d.added} −${d.removed}`
}
const hunks = (p: ScriptProposal, path: string): DiffLine[] => diffHunks(diffOf(p, path))
const SIGN = { added: '+', removed: '−', same: ' ', gap: '' } as const

// the files a proposal touches that were edited after it was written: applying replaces those edits
function edited(turn: Turn): string[] {
  const p = turn.proposal
  if (!p || !turn.sent) return []
  return [...p.changed, ...p.removed].filter((path) => (turn.sent![path] ?? null) !== contentNow(path))
}
// applied, and still as it left them: only then can it be taken back without losing a later edit
function canUndo(turn: Turn): boolean {
  const p = turn.proposal
  if (!p || !turn.before || turn.outcome !== 'applied' || props.readOnly) return false
  return p.changed.every((path) => contentNow(path) === (p.files.find((f) => f.path === path)?.content ?? null))
    && p.removed.every((path) => contentNow(path) === null)
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
  const sent = Object.fromEntries(props.files.map((f) => [f.path, f.content]))
  try {
    await ai.scriptAssist(
      {
        message, model: model.value, chat_id: chatId.value, environment_id: props.environmentId,
        files: props.files, entrypoint: props.entrypoint, variables: props.variables, inputs: props.inputs,
        output: props.output, saved: props.saved, locale: locale.value,
        // the gateway keeps only the error of this log for the model; in metadata-only mode the
        // log stays here (the gateway would drop it anyway) and only its exit code goes
        ...(withError || failed.value
          ? { run_log: metadataOnly.value ? '' : (run?.log ?? ''), run_exit_code: run?.exit_code ?? null }
          : {}),
      },
      {
        onText: (d) => { turn.text += d; scrollDown() },
        onToolCall: () => {},
        onToolResult: () => {},
        onDone: (done: AiDone) => {
          chatId.value = done.chat_id ?? chatId.value
          turn.usage = done.usage
          if (done.truncated) turn.error = t('aiCompliance.truncated')
          if (done.proposal) {
            turn.proposal = done.proposal
            turn.sent = sent
            turn.open = {}
            turn.outcome = 'pending'
          }
        },
        onError: (msg, _code, extra) => {
          turn.error = msg
          // the model declined: what it wrote before that is not an answer
          if (extra?.discard) turn.text = ''
        },
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
  const p = turn.proposal
  if (!p) return
  // what the card says and what Undo puts back are read now, before the files change under them
  turn.stats = Object.fromEntries(p.changed.map((path) => [path, stat(p, path)]))
  turn.before = {
    files: [...p.changed, ...p.removed].map((path) => ({ path, content: contentNow(path) })),
    entrypoint: props.entrypoint,
  }
  emit('apply', p)
  turn.outcome = 'applied'
}
function undo(turn: Turn) {
  if (!turn.before || !canUndo(turn)) return
  emit('revert', turn.before)
  turn.before = undefined
  turn.stats = undefined
  turn.outcome = 'pending'
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
    <AiDisclosure :open="accepted === false" :metadata-only="metadataOnly" :providers="aiProviders(status)" @accept="accept" />
    <header class="sa-head">
      <h3><Sparkles :size="14" /> {{ $t('scriptAssistant.title') }}</h3>
      <button v-if="turns.length" class="sa-mini" type="button" :disabled="busy" :title="$t('scriptAssistant.newConversation')"
              :aria-label="$t('scriptAssistant.newConversation')" @click="reset"><Plus :size="13" /></button>
    </header>

    <div ref="scroller" class="sa-stream" aria-live="polite">
      <div v-if="!turns.length" class="sa-empty">
        <p>{{ $t('scriptAssistant.emptyBody') }}</p>
        <p v-if="failed && metadataOnly" class="sa-note">{{ $t('scriptAssistant.errorWithheld') }}</p>
        <button v-else-if="failed" type="button" class="sa-fix" :disabled="!ready || busy" @click="send($t('scriptAssistant.fixPrompt'), true)">
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
          <AiSpeaker who="ai" :model="aiModelLabel(status, turn.model)" />
          <MarkdownLite v-if="turn.text" :text="turn.text" />
          <p v-if="turn.streaming" class="sa-activity"><LoaderCircle :size="12" class="spin" /> {{ $t('scriptAssistant.working') }}</p>
          <p v-if="turn.error" class="sa-note err"><CircleAlert :size="13" /> <span>{{ turn.error }}</span></p>
          <div v-if="turn.proposal" class="sa-proposal" :class="turn.outcome">
            <div class="sa-ptitle"><FileDiff :size="13" /> {{ $t('scriptAssistant.proposal') }}</div>
            <ul>
              <li v-for="p in turn.proposal.changed" :key="p">
                <template v-if="turn.outcome === 'pending' && turn.open">
                  <button type="button" class="sa-file" :aria-expanded="!!turn.open[p]" :title="$t('scriptAssistant.showChanges')"
                          @click="turn.open[p] = !turn.open[p]">
                    <ChevronRight :size="12" class="sa-chev" aria-hidden="true" /> <code>{{ p }}</code>
                    <span class="muted">{{ stat(turn.proposal, p) }}</span>
                  </button>
                  <ExpandBody>
                    <ol v-if="turn.open[p]" class="sa-diff" :aria-label="$t('scriptAssistant.changesOf', { file: p })">
                      <li v-for="(line, li) in hunks(turn.proposal, p)" :key="li" :class="line.kind">
                        <template v-if="line.kind === 'gap'">{{ $t('scriptAssistant.unchanged', { n: line.text }) }}</template>
                        <template v-else>
                          <span v-if="line.kind !== 'same'" class="sr-only">{{ $t(`scriptAssistant.line.${line.kind}`) }}</span><span aria-hidden="true" class="sa-sign">{{ SIGN[line.kind] }}</span>{{ line.text }}
                        </template>
                      </li>
                    </ol>
                  </ExpandBody>
                </template>
                <template v-else><code>{{ p }}</code> <span class="muted">{{ turn.stats?.[p] ?? stat(turn.proposal, p) }}</span></template>
              </li>
              <li v-for="p in turn.proposal.removed" :key="`r${p}`"><code>{{ p }}</code> <span class="muted">{{ $t('scriptAssistant.removed') }}</span></li>
              <li v-if="turn.proposal.entrypoint !== entrypoint"><span class="muted">{{ $t('scriptAssistant.runs', { file: turn.proposal.entrypoint }) }}</span></li>
            </ul>
            <template v-if="turn.outcome === 'pending'">
              <p v-if="edited(turn).length" class="sa-note warn" role="status">
                <TriangleAlert :size="13" aria-hidden="true" />
                <span>{{ $t('scriptAssistant.edited', { files: edited(turn).join(', ') }) }}</span>
              </p>
              <div class="sa-pactions">
                <button type="button" class="sa-mini-btn" :class="{ primary: !edited(turn).length }" @click="apply(turn)">
                  <Check :size="13" /> {{ $t(edited(turn).length ? 'scriptAssistant.applyAnyway' : 'scriptAssistant.apply') }}
                </button>
                <button type="button" class="sa-mini-btn" @click="turn.outcome = 'discarded'">{{ $t('scriptAssistant.discard') }}</button>
              </div>
            </template>
            <div v-else-if="turn.outcome === 'applied'" class="sa-pactions">
              <p class="sa-note ok">{{ $t('scriptAssistant.applied') }}</p>
              <button v-if="canUndo(turn)" type="button" class="sa-mini-btn" @click="undo(turn)"><Undo2 :size="13" /> {{ $t('scriptAssistant.undo') }}</button>
            </div>
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
      <p class="sa-notice"><Sparkles :size="12" aria-hidden="true" /> {{ metadataOnly ? $t('scriptAssistant.metadataOnly') : $t('scriptAssistant.noData') }}</p>
      <p v-if="sentTo" class="sa-notice">{{ $t('aiCompliance.sentTo', { provider: sentTo }) }}</p>
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
.sa-pactions { display: flex; align-items: center; gap: 6px; }
.sa-pactions .sa-note { margin: 0; flex: 1; }
.sa-note.warn { color: var(--warning); }
/* a file of the proposal: its name opens what would change in it */
.sa-file { display: flex; align-items: center; gap: 5px; width: 100%; min-height: 0; padding: 2px 4px; margin: 0 -4px; border: none; background: none;
  font-size: 12px; text-align: left; border-radius: 5px; }
.sa-file:hover { background: var(--bg-soft); box-shadow: none; }
.sa-chev { flex: none; color: var(--muted); transition: transform var(--dur-2) ease; }
.sa-file[aria-expanded="true"] .sa-chev { transform: rotate(90deg); }
/* the difference: the changed lines with two unchanged around them, in the editor's own face */
.sa-diff { list-style: none; margin: 4px 0 6px; padding: 6px 0; max-height: 280px; overflow: auto; border: 1px solid var(--border-soft); border-radius: 6px;
  background: var(--panel); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; line-height: 1.5; }
.sa-diff li { padding: 0 8px; white-space: pre; min-width: max-content; }
.sa-diff li.added { color: color-mix(in srgb, var(--accent-2) 70%, var(--text)); background: color-mix(in srgb, var(--accent-2) 10%, transparent); }
.sa-diff li.removed { color: color-mix(in srgb, var(--danger) 70%, var(--text)); background: color-mix(in srgb, var(--danger) 10%, transparent); }
.sa-diff li.gap { color: var(--muted); font-family: inherit; font-style: italic; padding-top: 2px; padding-bottom: 2px; }
.sa-sign { display: inline-block; width: 1.4ch; user-select: none; }
.sa-mini-btn { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; }
.sa-composer { padding-top: 8px; border-top: 1px solid var(--border-soft); display: flex; flex-direction: column; gap: 4px; }
.sa-field { display: flex; gap: 6px; align-items: flex-end; }
.sa-field textarea { flex: 1; min-width: 0; resize: vertical; font-size: 12.5px; }
.sa-send { padding: 6px 9px; }
.sa-notice { display: flex; align-items: flex-start; gap: 5px; margin: 0; font-size: 11px; line-height: 1.4; color: var(--muted); }
.sa-notice svg { flex: none; margin-top: 2px; color: var(--accent-hi); }
.sa-model { margin-top: 2px; }
</style>
