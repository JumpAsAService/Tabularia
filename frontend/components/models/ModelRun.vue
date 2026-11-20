<script setup lang="ts">
// «Run» of a model node (docs/specs/statistical-models.md, «The trial: Run in the editor»): the saved
// flow (saved first when it has changes) runs the node on the sources' development sample, in the
// trials' namespace, without the nodes after it; what it writes becomes their preview, and the
// node's Table and Report tabs. Below the button: the trial in progress and its seconds, then what
// the latest trial or run left — its outcome, the runner's sentence when it refused the data, the
// log, and «not up to date» when the node or a step before it changed since.
//
// The button commits the field still being edited first (commitFocusedField), as a Script node's
// does: a value typed a moment ago is the one that runs.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CheckCircle2, LoaderCircle, Play, TriangleAlert, XCircle } from 'lucide-vue-next'
import type { ScriptOutputs } from '~/composables/useScripting'
import { useFormat } from '~/composables/useFormat'
import { commitFocusedField } from '~/composables/commitFocusedField'

const props = defineProps<{
  outputs?: ScriptOutputs[string] | null
  trial?: { runId: number | null; status: string; startedAt: number; error: string } | null
  flowDirty?: boolean
  // the node cannot run now (its group is off for this person, it is not set yet), and what to say
  // of it here ('' when the form above says it already)
  disabled?: boolean
  why?: string
  readOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'run'): void }>()
const fmt = useFormat()

const running = computed(() => !!props.trial && (props.trial.status === 'PENDING' || props.trial.status === 'STARTED'))
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | undefined
// the seconds tick in the browser only (mounted): a page drawn on the server keeps no timer
onMounted(() => {
  watch(running, (on) => {
    clearInterval(ticker)
    if (on) ticker = setInterval(() => { now.value = Date.now() }, 1000)
  }, { immediate: true })
})
onBeforeUnmount(() => clearInterval(ticker))
const seconds = computed(() => (props.trial ? Math.max(0, Math.round((now.value - props.trial.startedAt) / 1000)) : 0))
const last = computed(() => props.outputs?.last ?? null)
// the trial's own message only when its run is not the last one shown (refused before it started,
// or the outputs not read yet): otherwise the last run says it, with its log
const failedEarly = computed(() => props.trial?.status === 'FAILURE' && !!props.trial.error
  && (props.trial.runId == null || last.value?.run_id !== props.trial.runId))
const canRun = computed(() => !props.readOnly && !props.disabled)

async function runFromPanel() {
  await commitFocusedField()
  emit('run')
}
</script>

<template>
  <div class="model-run">
    <button type="button" :disabled="!canRun || running" @mousedown.prevent @click="runFromPanel">
      <LoaderCircle v-if="running" :size="14" class="spin" aria-hidden="true" /><Play v-else :size="14" aria-hidden="true" />
      {{ running ? $t('models.node.running', { s: seconds }) : flowDirty ? $t('models.node.saveAndRun') : $t('models.node.run') }}
    </button>
    <p class="muted hint">{{ $t('models.node.runHint') }}</p>
    <p v-if="why && !readOnly" class="muted hint">{{ why }}</p>
    <p v-if="failedEarly" class="outcome ko" role="alert"><XCircle :size="13" aria-hidden="true" /> {{ trial!.error }}</p>
    <template v-if="last && !running">
      <p class="outcome" :class="last.status === 'SUCCESS' ? 'ok' : 'ko'">
        <component :is="last.status === 'SUCCESS' ? CheckCircle2 : XCircle" :size="13" aria-hidden="true" />
        {{ $t(last.trial ? 'models.node.lastTrial' : 'models.node.lastRun', { when: fmt.ago(last.finished_at ?? last.started_at), status: last.status }) }}
        <span v-if="outputs?.output?.rows != null && last.status === 'SUCCESS'">· {{ $t('models.node.rows', { n: fmt.count(outputs.output.rows) }) }}</span>
      </p>
      <p v-if="last.status !== 'SUCCESS' && last.error" class="muted hint error">{{ last.error }}</p>
      <p v-if="last.stale" class="warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('models.node.stale') }}</p>
      <details v-if="last.log">
        <summary>{{ $t('models.node.log') }}</summary>
        <pre class="log">{{ last.log }}</pre>
      </details>
    </template>
  </div>
</template>

<style scoped>
.model-run { display: flex; flex-direction: column; gap: 6px; }
.model-run button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
.hint { margin: 0; font-size: 12px; line-height: 1.5; }
.warn { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12px; color: var(--warning); }
.outcome { margin: 0; display: flex; gap: 6px; align-items: flex-start; flex-wrap: wrap; font-size: 12px; }
.outcome.ok svg { color: var(--accent-2); }
.outcome.ko, .outcome.ko svg { color: var(--danger); }
.error { overflow-wrap: anywhere; }
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.log { margin: 6px 0 0; max-height: 220px; overflow: auto; font-size: 11px; padding: 6px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
