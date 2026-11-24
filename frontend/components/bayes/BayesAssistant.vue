<script setup lang="ts">
// The assistant of the Bayesian model editor, a tab of the side panel (docs/specs/bayes-assistants.md,
// lots A3 and A4). The person asks; the assistant explains, and may propose changes of the graph: the
// distribution and arguments of a prior, an expression, a bound, the form of a box. A proposal is
// placed on the graph as it is now, read by the gateway's parser (`POST /bayes-models/check`) BEFORE
// «Apply» is offered, and applied as one change of the editor: the person's own undo takes it back.
// Nothing is saved, run or added: the assistant never adds or deletes a node.
//
// What goes to the model: the graph (without its drawing), the findings, the numbers of the prior
// charts of the selected node and, when the person chooses a run, the sampler's facts and each node's
// extremes of R-hat and effective sample size; never a row of data and never a posterior number.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowRight, Check, CircleAlert, CircleCheck, FileDiff, LoaderCircle, Plus, Send, Sparkles, Square, TriangleAlert } from 'lucide-vue-next'
import { aiModelLabel, aiModelOptions, aiProviderOf, aiProviders, useAi, type AiDone, type AiStatus, type AiUsage } from '~/composables/useAi'
import { useAiDisclosure } from '~/composables/useAiDisclosure'
import { useLocale } from '~/composables/useLocale'
import { useBayesWords } from '~/composables/useBayesWords'
import { useBayesPriorPreview } from '~/composables/bayesPriorPreview'
import { bayesGatewayFindings } from '~/composables/bayesModelSave'
import {
  bayesApplyUnits, bayesAssistBody, bayesAssistFits, bayesAssistPreview, bayesProposalUnits, bayesProposedGraph, bayesReadProposal,
  bayesRunChoices, bayesRunOtherVersion, bayesVerdictOf, type BayesProposal, type BayesRunChoice, type BayesRunFacts, type BayesRunListed, type BayesUnit,
} from '~/composables/bayesAssist'
import { bayesLimit } from '~/composables/bayesCatalog'
import type { BayesDoc } from '~/composables/bayesGraphEdits'
import type { BayesGraphFinding, BayesPriorPlan } from '~/composables/bayesGraph'
import type { BayesCheck } from '~/composables/useBayesModels'
import AiDisclosure from '~/components/ui/AiDisclosure.vue'
import MarkdownLite from '~/components/ui/MarkdownLite.vue'
import Select from '~/components/ui/Select.vue'
import AiSpeaker from '~/components/ui/AiSpeaker.vue'

/** The calls the tab makes of the gateway (the page hands in the store's client). */
export interface BayesAssistClient {
  check: (graph: Record<string, any>) => Promise<BayesCheck>
  runs: (id: number) => Promise<{ runs: BayesRunListed[], hidden: number }>
  runFacts: (id: number, run: number) => Promise<BayesRunFacts>
}

const props = defineProps<{
  status: AiStatus
  doc: BayesDoc
  findings: BayesGraphFinding[]
  selected: string | null
  plan: BayesPriorPlan | null
  /** The saved model (null: never saved, so no flow uses it and no run can be read). */
  modelId: number | null
  /** The version the editor holds (null: never saved). */
  version: number | null
  saved: boolean
  /** A version looked at: the assistant explains it and nothing is applied. */
  readOnly?: boolean
  /** The keys that undo, in the person's keyboard's words. */
  undoKeys: string
  client: BayesAssistClient
}>()
const emit = defineEmits<{ (e: 'apply', change: (doc: BayesDoc) => void): void }>()

const { t } = useI18n()
const { locale } = useLocale()
const ai = useAi()
const { accepted, read, accept } = useAiDisclosure()
const { labelOf, say, slotWords } = useBayesWords(() => props.doc)

/** What a check of a proposed graph found, for the whole proposal (`*`) or for one unit. */
interface Check { state: 'run' | 'done' | 'failed', added: BayesGraphFinding[], applicable: boolean }

interface Turn {
  question: string
  model: string
  text: string
  streaming: boolean
  error?: string
  usage?: AiUsage
  proposal?: BayesProposal | null
  outcome?: 'pending' | 'applied' | 'discarded' | 'outdated'
  /** The units as they stood when the proposal closed. */
  closed?: BayesUnit[]
  appliedKeys: string[]
  checks: Record<string, Check>
}

const MODEL_KEY = 'tabularia.bayesAssistant.model'
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
const metadataOnly = computed(() => !!props.status.metadata_only)
const fits = computed(() => bayesAssistFits(props.doc))
const ready = computed(() => accepted.value === true && !!model.value && fits.value)
const limitKib = computed(() => Math.round(bayesLimit('max_graph_bytes') / 1024))

// the numbers of the selected node's prior chart, drawn by the chart's own worker, one quantity after the other
const preview = useBayesPriorPreview(0)
function drawOne (target: string): Promise<{ q5: number, q50: number, q95: number } | null> {
  const plan = props.plan
  if (!plan) return Promise.resolve(null)
  return new Promise((resolve) => {
    let finished = false
    const finish = (v: { q5: number, q50: number, q95: number } | null) => {
      if (finished) return
      finished = true
      stop()
      clearTimeout(timer)
      resolve(v)
    }
    const stop = watch(preview.state, (s) => {
      if (s.failed) finish(null)
      else if (!s.pending && s.answer?.target === target) finish(s.answer.quantiles ?? null)
    })
    const timer = setTimeout(() => finish(null), 3000)
    preview.request(plan, target, 1)
  })
}

onMounted(() => {
  read(metadataOnly.value, aiProviders(props.status))
  let saved = ''
  try { saved = localStorage.getItem(MODEL_KEY) ?? '' } catch { /* no storage: the default model */ }
  model.value = props.status.models.includes(saved) ? saved : (props.status.default_model ?? props.status.models[0] ?? '')
})
// the editor closed while the model writes: the answer is stopped, not paid for to no one
onBeforeUnmount(() => { controller?.abort(); if (checkTimer) clearTimeout(checkTimer); checkSeq++ })
watch(model, (m) => { try { if (m) localStorage.setItem(MODEL_KEY, m) } catch { /* a convenience */ } })

function scrollDown () {
  nextTick(() => { if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight })
}

// ---------------------------------------------------------------- reading a run

const runList = ref<BayesRunChoice[]>([])
const runsHidden = ref(0)
const runsState = ref<'idle' | 'loading' | 'done' | 'failed'>('idle')
const chosen = ref('')
const facts = ref<BayesRunFacts | null>(null)
const factsState = ref<'idle' | 'loading' | 'failed'>('idle')
let factsSeq = 0

async function loadRuns () {
  if (props.modelId === null || runsState.value === 'loading') return
  runsState.value = 'loading'
  try {
    const r = await props.client.runs(props.modelId)
    runList.value = bayesRunChoices(r.runs ?? [])
    runsHidden.value = r.hidden ?? 0
    runsState.value = 'done'
  } catch {
    runsState.value = 'failed'
  }
}
function onRunsToggle (e: Event) {
  if ((e.target as HTMLDetailsElement).open && runsState.value !== 'loading') void loadRuns()
}
async function choose (key: string) {
  chosen.value = key
  const mine = ++factsSeq
  facts.value = null
  factsState.value = 'idle'
  if (!key || props.modelId === null) return
  const entry = runList.value.find(r => r.key === key)?.entry
  if (!entry || entry.run_id === null) return
  factsState.value = 'loading'
  try {
    const f = await props.client.runFacts(props.modelId, entry.run_id)
    if (mine !== factsSeq) return
    facts.value = f
    factsState.value = 'idle'
  } catch {
    if (mine !== factsSeq) return
    chosen.value = ''
    factsState.value = 'failed'
  }
}
const chosenEntry = computed(() => runList.value.find(r => r.key === chosen.value)?.entry ?? null)
const otherVersion = computed(() => bayesRunOtherVersion(facts.value?.version, props.version))
// a different model on the page: what was listed is not this model's
watch(() => props.modelId, () => { runList.value = []; runsState.value = 'idle'; chosen.value = ''; facts.value = null; factsState.value = 'idle' })

// ---------------------------------------------------------------- the proposal

/** The proposal the person may still apply: the last turn that holds one. */
const active = computed<Turn | null>(() => {
  for (let i = turns.value.length - 1; i >= 0; i--) {
    const x = turns.value[i]!
    if (x.outcome === 'pending' && x.proposal) return x
  }
  return null
})
const activeUnits = computed<BayesUnit[]>(() => (active.value?.proposal ? bayesProposalUnits(props.doc, active.value.proposal) : []))
const unitsOf = (turn: Turn): BayesUnit[] => (turn === active.value ? activeUnits.value : (turn.closed ?? []))
const openUnits = computed(() => activeUnits.value.filter(u => u.place === 'ok'))
// the graph as text: when it changes under a proposal, the check is made again
const docText = computed(() => (active.value ? JSON.stringify(props.doc) : ''))

let checkSeq = 0
let checkTimer: ReturnType<typeof setTimeout> | null = null

async function runChecks () {
  const turn = active.value
  const mine = ++checkSeq
  if (!turn) return
  const open = openUnits.value
  const jobs: Array<{ key: string, units: BayesUnit[] }> = []
  if (open.length) jobs.push({ key: '*', units: open })
  if (open.length > 1) for (const u of open) jobs.push({ key: u.key, units: [u] })
  turn.checks = {}
  for (const j of jobs) turn.checks[j.key] = { state: 'run', added: [], applicable: false }
  for (const j of jobs) {
    try {
      const graph = JSON.parse(JSON.stringify(bayesProposedGraph(props.doc, j.units))) as Record<string, any>
      const answer = await props.client.check(graph)
      if (mine !== checkSeq) return
      const v = bayesVerdictOf(props.findings, bayesGatewayFindings(answer.findings))
      turn.checks[j.key] = { state: 'done', added: v.added, applicable: v.applicable }
    } catch {
      if (mine !== checkSeq) return
      turn.checks[j.key] = { state: 'failed', added: [], applicable: false }
    }
  }
}
function scheduleChecks (delay: number) {
  if (checkTimer) clearTimeout(checkTimer)
  checkSeq++
  checkTimer = setTimeout(() => { checkTimer = null; void runChecks() }, delay)
}
// a proposal arrives: its graph is read at once; the graph changes under it: after a moment
watch(() => active.value?.proposal, (p) => { if (p) scheduleChecks(0) })
watch(docText, (v, old) => { if (v && old && v !== old) scheduleChecks(500) })

const checkOf = (turn: Turn, key: string): Check | undefined => turn.checks[key]
const canApply = (turn: Turn, key: string): boolean => {
  const c = checkOf(turn, key)
  return !props.readOnly && c?.state === 'done' && c.applicable
}

function snapshot (turn: Turn) { turn.closed = activeUnits.value.map(u => ({ ...u })) }

function apply (turn: Turn, which: BayesUnit[]) {
  const units = which.filter(u => u.place === 'ok')
  if (!units.length || props.readOnly) return
  emit('apply', (d) => { bayesApplyUnits(d, units) })
  for (const u of units) turn.appliedKeys.push(u.key)
  // what is left to apply, against the graph as the edit made it
  nextTick(() => {
    if (turn === active.value && !openUnits.value.length) {
      snapshot(turn)
      turn.outcome = 'applied'
    }
  })
}
function discard (turn: Turn) {
  snapshot(turn)
  turn.outcome = 'discarded'
}

/** The words of what a unit changes: «Prior», «Bounds», «Form», or the slot's («σ of the prior»). */
function fieldWords (u: BayesUnit): string {
  if (u.kind === 'expression') return u.slot ? slotWords(u.slot) : ''
  return t(`bayesAssistant.kind.${u.kind}`)
}
function shownValue (u: BayesUnit, v: string): string {
  if (u.kind === 'form') return t(`bayesAssistant.form.${v === 'centred' ? 'centred' : 'non_centred'}`)
  if (!v) return u.kind === 'bounds' ? t('bayesAssistant.noBounds') : '…'
  return v
}
const findingLine = (f: BayesGraphFinding): string => {
  const where = f.node ? `${labelOf(f.node)}: ` : ''
  return `${where}${say(f)}`
}

function sendBack (turn: Turn) {
  const list = (turn.checks['*']?.added ?? []).map(f => `- ${findingLine(f)}`).join('\n')
  if (list) void send(t('bayesAssistant.findingsMessage', { list }))
}

// ---------------------------------------------------------------- asking

const examples = computed(() => [props.selected ? t('bayesAssistant.example1Selected') : t('bayesAssistant.example1'), t('bayesAssistant.example2')])

async function send (text?: string) {
  const message = (text ?? draft.value).trim()
  if (!message || busy.value || !ready.value) return
  // built on an earlier graph: no longer to be applied
  for (const x of turns.value) if (x.outcome === 'pending') { if (x === active.value) snapshot(x); x.outcome = 'outdated' }
  const turn = reactive<Turn>({ question: message, model: model.value, text: '', streaming: true, appliedKeys: [], checks: {} })
  turns.value.push(turn)
  draft.value = ''
  busy.value = true
  controller = new AbortController()
  scrollDown()
  const sel = props.selected
  const body = bayesAssistBody({
    message, model: model.value, chatId: chatId.value, doc: props.doc, findings: props.findings, say,
    selected: sel,
    preview: await bayesAssistPreview(props.plan, sel, drawOne),
    run: facts.value, version: props.version, saved: props.saved, locale: locale.value,
  })
  try {
    await ai.bayesAssist(body, {
      onText: (d) => { turn.text += d; scrollDown() },
      onToolCall: () => {},
      onToolResult: () => {},
      onDone: (done: AiDone) => {
        chatId.value = done.chat_id ?? chatId.value
        turn.usage = done.usage
        if (done.truncated) turn.error = t('aiCompliance.truncated')
        const p = bayesReadProposal(done.proposal as unknown)
        if (p) { turn.proposal = p; turn.outcome = 'pending' }
      },
      onError: (msg, _code, extra) => {
        turn.error = msg
        // the model declined: what it wrote before that is not an answer
        if (extra?.discard) turn.text = ''
      },
    }, controller.signal)
  } catch (e: any) {
    turn.error = e?.name === 'AbortError' ? t('bayesAssistant.stopped') : String(e?.message ?? e)
  } finally {
    turn.streaming = false
    busy.value = false
    controller = null
    scrollDown()
    nextTick(() => input.value?.focus())
  }
}

function reset () {
  if (busy.value) return
  turns.value = []
  chatId.value = null
}
function onKey (e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    void send()
  }
}
</script>

<template>
  <section class="ba" :aria-label="t('bayesAssistant.title')">
    <AiDisclosure :open="accepted === false" :metadata-only="metadataOnly" :providers="aiProviders(status)" @accept="accept" />
    <header class="ba-head">
      <h2><Sparkles :size="14" aria-hidden="true" /> {{ t('bayesAssistant.title') }}</h2>
      <button v-if="turns.length" class="ba-mini" type="button" :disabled="busy" :title="t('bayesAssistant.newConversation')"
              :aria-label="t('bayesAssistant.newConversation')" @click="reset"><Plus :size="13" aria-hidden="true" /></button>
    </header>

    <!-- «Read a run»: the Bayesian nodes of flows that pin this model (lot A4) -->
    <details v-if="modelId !== null" class="ba-run" @toggle="onRunsToggle">
      <summary>
        <span class="ba-run-label">{{ t('bayesAssistant.run.label') }}</span>
        <span class="ba-run-now">{{ chosenEntry ? `${chosenEntry.flow_name} · ${chosenEntry.node_label}` : t('bayesAssistant.run.none') }}</span>
      </summary>
      <div class="ba-run-body">
        <p class="ba-hint">{{ t('bayesAssistant.run.hint') }}</p>
        <p v-if="runsState === 'loading'" class="ba-hint"><LoaderCircle :size="12" class="spin" aria-hidden="true" /> {{ t('bayesAssistant.run.loading') }}</p>
        <p v-else-if="runsState === 'failed'" class="ba-hint err" role="status"><CircleAlert :size="12" aria-hidden="true" /> {{ t('bayesAssistant.run.listFailed') }}</p>
        <fieldset v-else-if="runsState === 'done'" class="ba-runs">
          <legend class="sr-only">{{ t('bayesAssistant.run.label') }}</legend>
          <label class="ba-pick" :class="{ on: chosen === '' }">
            <input type="radio" name="ba-run" value="" :checked="chosen === ''" @change="choose('')">
            <span class="ba-pick-main">{{ t('bayesAssistant.run.none') }}</span>
          </label>
          <label v-for="r in runList" :key="r.key" class="ba-pick" :class="{ on: chosen === r.key, off: r.disabled }">
            <input type="radio" name="ba-run" :value="r.key" :checked="chosen === r.key" :disabled="r.disabled" @change="choose(r.key)">
            <span class="ba-pick-main">{{ r.entry.flow_name }} · {{ r.entry.node_label }}</span>
            <span class="ba-pick-sub">
              <template v-if="r.disabled">{{ t('bayesAssistant.run.never') }}</template>
              <template v-else>v{{ r.entry.version }}<template v-if="r.trial"> · {{ t('bayesAssistant.run.trial') }}</template></template>
            </span>
          </label>
          <p v-if="!runList.length" class="ba-hint">{{ t('bayesAssistant.run.empty') }}</p>
          <p v-if="runsHidden" class="ba-hint">{{ t('bayesAssistant.run.hidden', { n: runsHidden }) }}</p>
        </fieldset>
      </div>
    </details>
    <p v-if="factsState === 'loading'" class="ba-note" role="status"><LoaderCircle :size="12" class="spin" aria-hidden="true" /> {{ t('bayesAssistant.run.loading') }}</p>
    <p v-else-if="factsState === 'failed'" class="ba-note err" role="status"><CircleAlert :size="13" aria-hidden="true" /> <span>{{ t('bayesAssistant.run.failed') }}</span></p>
    <p v-else-if="facts && otherVersion" class="ba-note warn" role="status">
      <TriangleAlert :size="13" aria-hidden="true" />
      <span>{{ t('bayesAssistant.run.version', { run: facts.version, editor: version ?? '' }) }}</span>
    </p>

    <div ref="scroller" class="ba-stream" aria-live="polite">
      <div v-if="!turns.length" class="ba-empty">
        <p>{{ t('bayesAssistant.emptyBody') }}</p>
        <p v-if="!fits" class="ba-note err"><CircleAlert :size="13" aria-hidden="true" /> <span>{{ t('bayesAssistant.tooBig', { limit: limitKib }) }}</span></p>
        <button v-for="(ex, i) in examples" :key="i" type="button" class="ba-example" :disabled="!ready || busy" @click="send(ex)">{{ ex }}</button>
        <p v-if="readOnly" class="ba-note">{{ t('bayesAssistant.readOnly') }}</p>
      </div>

      <template v-for="(turn, ti) in turns" :key="ti">
        <div class="ba-you">
          <AiSpeaker who="you" />
          <p class="ba-q">{{ turn.question }}</p>
        </div>
        <div class="ba-a" role="group" :aria-label="t('aiCompliance.assistantLabel')">
          <AiSpeaker who="ai" :model="aiModelLabel(status, turn.model)" />
          <MarkdownLite v-if="turn.text" :text="turn.text" />
          <p v-if="turn.streaming" class="ba-activity"><LoaderCircle :size="12" class="spin" aria-hidden="true" /> {{ t('bayesAssistant.working') }}</p>
          <p v-if="turn.error" class="ba-note err"><CircleAlert :size="13" aria-hidden="true" /> <span>{{ turn.error }}</span></p>

          <div v-if="turn.proposal" class="ba-proposal" :class="turn.outcome">
            <div class="ba-ptitle"><FileDiff :size="13" aria-hidden="true" /> {{ t('bayesAssistant.proposal') }}</div>
            <p v-if="turn.proposal.summary" class="ba-summary">{{ turn.proposal.summary }}</p>
            <ul class="ba-units">
              <li v-for="u in unitsOf(turn)" :key="u.key" class="ba-unit">
                <div class="ba-on">
                  <strong>{{ labelOf(u.node) }}</strong>
                  <span class="ba-kind">{{ fieldWords(u) }}</span>
                </div>
                <div class="ba-diff">
                  <span class="sr-only">{{ t('bayesAssistant.from') }}</span>
                  <code class="ba-from">{{ shownValue(u, u.before) }}</code>
                  <ArrowRight :size="12" aria-hidden="true" />
                  <span class="sr-only">{{ t('bayesAssistant.to') }}</span>
                  <code class="ba-to">{{ shownValue(u, u.after) }}</code>
                </div>
                <p v-if="u.why" class="ba-why">{{ u.why }}</p>
                <p v-if="turn.appliedKeys.includes(u.key) && turn.outcome === 'pending'" class="ba-note ok"><Check :size="12" aria-hidden="true" /> {{ t('bayesAssistant.applied', { keys: undoKeys }) }}</p>
                <p v-else-if="u.place !== 'ok' && !turn.appliedKeys.includes(u.key)" class="ba-note"><CircleAlert :size="12" aria-hidden="true" /> <span>{{ t(`bayesAssistant.place.${u.place}`) }}</span></p>
                <template v-else-if="turn === active && unitsOf(turn).length > 1">
                  <ul v-if="checkOf(turn, u.key)?.added.length" class="ba-found">
                    <li v-for="(f, i) in checkOf(turn, u.key)!.added" :key="i" :class="f.severity">
                      <component :is="f.severity === 'error' ? CircleAlert : TriangleAlert" :size="12" aria-hidden="true" /><span>{{ findingLine(f) }}</span>
                    </li>
                  </ul>
                  <button type="button" class="ba-mini-btn ba-one" :disabled="!canApply(turn, u.key)" :aria-label="`${t('bayesAssistant.apply')}: ${labelOf(u.node)}`" @click="apply(turn, [u])">
                    <Check :size="13" aria-hidden="true" /> {{ t('bayesAssistant.apply') }}
                  </button>
                </template>
              </li>
            </ul>

            <!-- what the gateway's parser says of the graph this would make, before «Apply» is offered -->
            <template v-if="turn === active && openUnits.length">
              <p v-if="!checkOf(turn, '*') || checkOf(turn, '*')!.state === 'run'" class="ba-note" role="status"><LoaderCircle :size="12" class="spin" aria-hidden="true" /> {{ t('bayesAssistant.checking') }}</p>
              <p v-else-if="checkOf(turn, '*')!.state === 'failed'" class="ba-note err" role="status"><CircleAlert :size="13" aria-hidden="true" /> <span>{{ t('bayesAssistant.checkFailed') }}</span></p>
              <template v-else>
                <p v-if="!checkOf(turn, '*')!.applicable" class="ba-note err" role="status"><CircleAlert :size="13" aria-hidden="true" /> <span>{{ t('bayesAssistant.checkRefused') }}</span></p>
                <p v-else-if="checkOf(turn, '*')!.added.length" class="ba-note" role="status"><TriangleAlert :size="13" aria-hidden="true" /> <span>{{ t('bayesAssistant.checkFindings') }}</span></p>
                <p v-else class="ba-note" role="status"><CircleCheck :size="13" aria-hidden="true" /> <span>{{ t('bayesAssistant.checkClean') }}</span></p>
                <ul v-if="checkOf(turn, '*')!.added.length" class="ba-found">
                  <li v-for="(f, i) in checkOf(turn, '*')!.added" :key="i" :class="f.severity">
                    <component :is="f.severity === 'error' ? CircleAlert : TriangleAlert" :size="12" aria-hidden="true" /><span>{{ findingLine(f) }}</span>
                  </li>
                </ul>
              </template>
            </template>

            <div v-if="turn.outcome === 'pending'" class="ba-pactions">
              <button v-if="openUnits.length" type="button" class="primary ba-mini-btn" :disabled="!canApply(turn, '*')" @click="apply(turn, openUnits)">
                <Check :size="13" aria-hidden="true" /> {{ unitsOf(turn).length > 1 ? t('bayesAssistant.applyAll') : t('bayesAssistant.apply') }}
              </button>
              <button v-if="checkOf(turn, '*')?.added.length" type="button" class="ba-mini-btn" :disabled="busy || !ready" @click="sendBack(turn)">{{ t('bayesAssistant.sendFindings') }}</button>
              <button type="button" class="ba-mini-btn" @click="discard(turn)">{{ t('bayesAssistant.discard') }}</button>
            </div>
            <p v-else-if="turn.outcome === 'applied'" class="ba-note ok"><Check :size="12" aria-hidden="true" /> {{ t('bayesAssistant.applied', { keys: undoKeys }) }}</p>
            <p v-else-if="turn.outcome === 'outdated'" class="ba-note">{{ t('bayesAssistant.outdated') }}</p>
            <p v-else class="ba-note">{{ t('bayesAssistant.discarded') }}</p>
          </div>
        </div>
      </template>
    </div>

    <form class="ba-composer" @submit.prevent="send()">
      <div class="ba-field">
        <textarea ref="input" v-model="draft" rows="2" :placeholder="t('bayesAssistant.placeholder')"
                  :aria-label="t('bayesAssistant.inputLabel')" :disabled="!ready" @keydown="onKey" />
        <button v-if="busy" type="button" class="ba-send" :title="t('bayesAssistant.stop')" :aria-label="t('bayesAssistant.stop')"
                @click="controller?.abort()"><Square :size="13" aria-hidden="true" /></button>
        <button v-else type="submit" class="ba-send primary" :disabled="!ready || !draft.trim()" :title="t('bayesAssistant.send')"
                :aria-label="t('bayesAssistant.send')"><Send :size="13" aria-hidden="true" /></button>
      </div>
      <p class="ba-notice"><Sparkles :size="12" aria-hidden="true" /> {{ facts ? t('bayesAssistant.noDataRun') : t('bayesAssistant.noData') }}</p>
      <p v-if="sentTo" class="ba-notice">{{ t('aiCompliance.sentTo', { provider: sentTo }) }}</p>
      <p class="ba-notice">{{ t('aiCompliance.disclaimer') }}</p>
      <Select v-if="modelOptions.length > 1" v-model="model" class="ba-model" :options="modelOptions" :disabled="busy"
              :aria-label="t('bayesAssistant.model')" />
    </form>
  </section>
</template>

<style scoped>
.ba { display: flex; flex-direction: column; min-height: 0; height: 100%; min-width: 0; }
.ba > .ba-note, .ba > .ba-run, .ba-head, .ba-composer { flex: none; }
.ba-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 0 6px; }
.ba-head h2 { display: inline-flex; align-items: center; gap: 6px; margin: 0; font-size: 14px; font-weight: 700; }
.ba-head h2 svg { color: var(--accent-hi); }
.ba-mini { padding: 2px 6px; }
.ba-run { margin: 0 0 6px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); }
.ba-run summary { display: flex; align-items: baseline; gap: 8px; padding: 6px 10px; font-size: 12px; cursor: pointer; min-width: 0; }
.ba-run summary:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: 8px; }
.ba-run-label { flex: none; font-weight: 600; }
.ba-run-now { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.ba-run-body { padding: 0 10px 8px; }
.ba-hint { display: flex; align-items: flex-start; gap: 6px; margin: 4px 0 6px; font-size: 11.5px; line-height: 1.45; color: var(--muted); }
.ba-hint.err { color: var(--danger-text); }
.ba-runs { border: 0; margin: 0; padding: 0; min-width: 0; display: flex; flex-direction: column; gap: 2px; max-height: 190px; overflow-y: auto; }
.ba-pick { display: grid; grid-template-columns: 16px 1fr; column-gap: 8px; align-items: baseline; padding: 5px 6px; border-radius: 6px; font-size: 12.5px; cursor: pointer; }
.ba-pick input { margin: 0; accent-color: var(--accent); }
.ba-pick-main { min-width: 0; overflow-wrap: anywhere; }
.ba-pick-sub { grid-column: 2; font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
.ba-pick.on { background: var(--tint-accent); }
.ba-pick.off { cursor: default; color: var(--muted); }
.ba-pick:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: -2px; }
/* The stream scrolls to its end, so a long answer's first line sits half cut at the top edge, right under the notes above:
   it read as text drawn over the warning. The text now fades out under the edge, and the padding keeps the first line
   clear of the fade when the stream is at its top. */
.ba-stream { flex: 1; min-height: 0; overflow-y: auto; padding: 10px 2px 0 0; margin-top: 4px;
  -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 12px); mask-image: linear-gradient(to bottom, transparent 0, #000 12px); }
.ba-empty p { margin: 2px 0 10px; font-size: 12.5px; line-height: 1.5; color: var(--muted); max-width: 62ch; }
.ba-example { display: flex; align-items: center; gap: 6px; width: 100%; margin-bottom: 6px; padding: 7px 10px; font-size: 12.5px; text-align: left; line-height: 1.4; border-radius: 8px; }
.ba-you { display: flex; flex-direction: column; align-items: flex-end; margin-top: 12px; }
.ba-q { margin: 0 0 6px; width: fit-content; max-width: 92%; padding: 7px 10px; border-radius: 10px 10px 3px 10px;
  background: var(--panel-2); border: 1px solid var(--border-soft); font-size: 12.5px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
.ba-a { margin-top: 6px; font-size: 12.5px; line-height: 1.5; overflow-wrap: anywhere; }
.ba-a :deep(p) { margin: 6px 0; }
.ba-a :deep(pre) { max-height: 260px; overflow: auto; font-size: 11.5px; }
.ba-activity { display: flex; align-items: center; gap: 6px; margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.ba-note { display: flex; align-items: flex-start; gap: 6px; margin: 6px 0 0; font-size: 12px; line-height: 1.45; color: var(--muted); }
.ba-note svg { flex: none; margin-top: 2px; }
.ba-note.err { color: var(--danger-text); }
.ba-note.ok { color: var(--accent-2); }
.ba-note.warn { color: var(--text); }
.ba-note.warn svg { color: var(--warning); }
.ba-proposal { margin-top: 8px; padding: 8px 10px; border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border)); border-radius: 9px;
  background: color-mix(in srgb, var(--accent) 6%, transparent); }
.ba-proposal.discarded, .ba-proposal.outdated { opacity: 0.65; }
.ba-ptitle { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }
.ba-summary { margin: 4px 0 0; font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }
.ba-units { margin: 6px 0 0; padding: 0; list-style: none; }
.ba-unit { padding: 8px 0; border-top: 1px solid var(--border-soft); font-size: 12px; min-width: 0; }
.ba-on { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
.ba-on strong { font-weight: 600; overflow-wrap: anywhere; }
.ba-kind { font-size: 11.5px; color: var(--muted); }
.ba-diff { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 6px; margin-top: 4px; }
.ba-diff code { font-size: 11.5px; overflow-wrap: anywhere; white-space: pre-wrap; }
.ba-from { color: var(--muted); }
.ba-diff svg { flex: none; color: var(--muted); }
.ba-why { margin: 4px 0 0; font-size: 12px; line-height: 1.45; color: var(--muted); overflow-wrap: anywhere; }
.ba-found { margin: 6px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
.ba-found li { display: flex; align-items: flex-start; gap: 6px; line-height: 1.4; overflow-wrap: anywhere; }
.ba-found li svg { flex: none; margin-top: 2px; }
.ba-found li.error svg { color: var(--danger-text); }
.ba-found li.warning svg { color: var(--warning); }
.ba-pactions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ba-mini-btn { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; }
.ba-one { margin-top: 6px; }
.ba-composer { padding-top: 8px; border-top: 1px solid var(--border-soft); display: flex; flex-direction: column; gap: 4px; }
.ba-field { display: flex; gap: 6px; align-items: flex-end; }
.ba-field textarea { flex: 1; min-width: 0; resize: vertical; font-size: 12.5px; }
.ba-send { padding: 6px 9px; }
.ba-notice { display: flex; align-items: flex-start; gap: 5px; margin: 0; font-size: 11px; line-height: 1.4; color: var(--muted); }
.ba-notice svg { flex: none; margin-top: 2px; color: var(--accent-hi); }
.ba-model { margin-top: 2px; }
</style>
