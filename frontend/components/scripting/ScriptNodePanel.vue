<script setup lang="ts">
// The node panel of a Script node: its environment, its timeout and retries, and the
// button that opens the code editor. What the node reads and writes comes from its
// edges: an edge in gives it TABULARIA_INPUT, an edge out asks it for
// TABULARIA_OUTPUT, neither is a step that runs something outside Tabularia.
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, Code2, FileCode2, LoaderCircle, Play, Trash2, TriangleAlert, XCircle } from 'lucide-vue-next'
import type { Node } from '@vue-flow/core'
import { useScriptingEditor, type ScriptOutputs } from '~/composables/useScripting'
import { useFormat } from '~/composables/useFormat'
import { starterFiles } from '~/composables/scriptTemplates'
import Select from '~/components/ui/Select.vue'

// Monaco weighs a few MB: loaded when the editor opens, not with the page
const ScriptEditor = defineAsyncComponent(() => import('~/components/scripting/ScriptEditor.vue'))

const props = defineProps<{
  node: Node
  readOnly?: boolean
  outputs?: ScriptOutputs[string] | null
  trial?: { runId: number | null; status: string; startedAt: number; error: string } | null
  flowDirty?: boolean
}>()
const emit = defineEmits<{
  (e: 'update', patch: Record<string, any>): void
  (e: 'delete'): void
  (e: 'run'): void
}>()
const fmt = useFormat()
const { t } = useI18n()
const scripting = useScriptingEditor()
onMounted(() => { scripting.load() })

const data = computed(() => props.node.data ?? {})
const environments = computed(() => scripting.state.value?.environments ?? [])
const environment = computed(() => environments.value.find((e) => e.id === data.value.environmentId) ?? null)
const files = computed<{ path: string; content: string }[]>(() => data.value.files ?? [])
const editorOpen = ref(false)

// the variables and packages may have changed since the editor page opened
async function openEditor() {
  await scripting.load(true)
  editorOpen.value = true
}

const options = computed(() => environments.value.map((e) => ({
  value: e.id,
  label: e.name,
  hint: `${e.language === 'r' ? 'R' : 'Python'} ${e.language_version ?? ''}${e.ready ? '' : ` · ${t('scripting.node.notReady')}`}`,
})))

function pickEnvironment(id: number | null) {
  const env = environments.value.find((e) => e.id === id)
  if (!env) return
  const patch: Record<string, any> = { environmentId: env.id, environmentName: env.name, language: env.language }
  // a node without code yet starts from the template of its language
  if (!files.value.length || (data.value.language && data.value.language !== env.language && !edited())) {
    const starter = starterFiles(env.language)
    patch.files = starter
    patch.entrypoint = starter[0]!.path
  }
  if ((data.value.timeoutSeconds ?? 900) > env.max_timeout_seconds) patch.timeoutSeconds = env.max_timeout_seconds
  emit('update', patch)
}

// the node still holds only its language's template
function edited(): boolean {
  const starter = starterFiles(data.value.language === 'r' ? 'r' : 'python')
  return JSON.stringify(files.value) !== JSON.stringify(starter)
}

function numberField(key: string, value: string, min: number, max: number) {
  const n = Math.round(Number(value))
  if (!Number.isFinite(n)) return
  emit('update', { [key]: Math.min(max, Math.max(min, n)) })
}

// «Run»: the trial in progress, its seconds, and what the last run of the node left
const running = computed(() => !!props.trial && (props.trial.status === 'PENDING' || props.trial.status === 'STARTED'))
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | undefined
watch(running, (on) => {
  clearInterval(ticker)
  if (on) ticker = setInterval(() => { now.value = Date.now() }, 1000)
}, { immediate: true })
onBeforeUnmount(() => clearInterval(ticker))
const seconds = computed(() => (props.trial ? Math.max(0, Math.round((now.value - props.trial.startedAt) / 1000)) : 0))
const last = computed(() => props.outputs?.last ?? null)
const canRun = computed(() => !props.readOnly && !!environment.value?.ready && files.value.length > 0 && !unavailable.value)

const timeoutMax = computed(() => environment.value?.max_timeout_seconds ?? 86400)
const unavailable = computed(() => {
  const s = scripting.state.value
  if (!s) return ''
  if (s.disabled_reason) return t('scripting.node.off')
  if (!s.storage_ready) return t('scripting.node.noBucket')
  if (data.value.environmentId != null && !environment.value) return t('scripting.node.environmentGone')
  if (environment.value && !environment.value.ready) return t('scripting.node.environmentNotReady', { name: environment.value.name })
  return ''
})
</script>

<template>
  <div class="script-panel">
    <div class="row">
      <h3><FileCode2 :size="15" /> {{ $t('scripting.node.title') }}</h3>
      <button v-if="!readOnly" class="del" :title="$t('scripting.node.delete')" @click="emit('delete')"><Trash2 :size="14" /></button>
    </div>
    <p class="muted outhint">{{ $t('scripting.node.hint') }}</p>
    <p v-if="unavailable" class="warn" role="note"><TriangleAlert :size="13" /> {{ unavailable }}</p>

    <label>{{ $t('scripting.node.name') }}</label>
    <input
      type="text"
      :value="data.label || ''"
      maxlength="80"
      :disabled="readOnly"
      @change="emit('update', { label: ($event.target as HTMLInputElement).value.trim() || t('scripting.node.title') })"
    />

    <label>{{ $t('scripting.node.environment') }}</label>
    <Select
      :model-value="data.environmentId ?? null"
      :options="options"
      :placeholder="$t('scripting.node.chooseEnvironment')"
      :disabled="readOnly"
      @update:model-value="pickEnvironment"
    />

    <div class="code">
      <div class="code-files">
        <span v-if="files.length" class="muted">
          {{ $t('scripting.node.files', { n: files.length }) }} · {{ $t('scripting.node.runs', { entry: data.entrypoint || '—' }) }}
        </span>
        <span v-else class="muted">{{ $t('scripting.node.noFiles') }}</span>
      </div>
      <button type="button" class="primary" :disabled="!environment" @click="openEditor">
        <Code2 :size="14" /> {{ readOnly ? $t('scripting.node.viewCode') : $t('scripting.node.editCode') }}
      </button>
    </div>

    <div class="trial">
      <button type="button" :disabled="!canRun || running" @click="emit('run')">
        <LoaderCircle v-if="running" :size="14" class="spin" /><Play v-else :size="14" />
        {{ running ? $t('scripting.node.running', { s: seconds }) : flowDirty ? $t('scripting.node.saveAndRun') : $t('scripting.node.run') }}
      </button>
      <p class="muted outhint">{{ $t('scripting.node.runHint') }}</p>
      <p v-if="trial?.status === 'FAILURE'" class="esito ko" role="alert"><XCircle :size="13" /> {{ trial.error }}</p>
      <template v-else-if="last && !running">
        <p class="esito" :class="last.status === 'SUCCESS' ? 'ok' : 'ko'">
          <component :is="last.status === 'SUCCESS' ? CheckCircle2 : XCircle" :size="13" />
          {{ $t(last.trial ? 'scripting.node.lastTrial' : 'scripting.node.lastRun', { when: fmt.ago(last.finished_at ?? last.started_at), status: last.status }) }}
          <span v-if="outputs?.output?.rows != null && last.status === 'SUCCESS'">· {{ $t('scripting.node.rows', { n: fmt.count(outputs.output.rows) }) }}</span>
        </p>
        <p v-if="last.stale" class="warn"><TriangleAlert :size="13" /> {{ $t('scripting.node.codeChanged') }}</p>
        <details v-if="last.log">
          <summary>{{ $t('scripting.node.log') }}</summary>
          <pre class="log">{{ last.log }}</pre>
        </details>
      </template>
    </div>

    <div class="numbers">
      <label>
        <span>{{ $t('scripting.node.timeout') }}</span>
        <input type="number" min="10" :max="timeoutMax" :value="data.timeoutSeconds ?? 900" :disabled="readOnly"
               @change="numberField('timeoutSeconds', ($event.target as HTMLInputElement).value, 10, timeoutMax)" />
      </label>
      <label>
        <span>{{ $t('scripting.node.retries') }}</span>
        <input type="number" min="0" max="5" :value="data.retries ?? 0" :disabled="readOnly"
               @change="numberField('retries', ($event.target as HTMLInputElement).value, 0, 5)" />
      </label>
      <label>
        <span>{{ $t('scripting.node.retryDelay') }}</span>
        <input type="number" min="0" max="3600" :value="data.retryDelaySeconds ?? 60" :disabled="readOnly || !(data.retries > 0)"
               @change="numberField('retryDelaySeconds', ($event.target as HTMLInputElement).value, 0, 3600)" />
      </label>
    </div>
    <p class="muted outhint">{{ $t('scripting.node.ioHint') }}</p>

    <ScriptEditor
      v-if="editorOpen && environment"
      :open="editorOpen"
      :title="data.label || t('scripting.node.title')"
      :environment="environment"
      :files="files"
      :entrypoint="data.entrypoint || ''"
      :tabularia-variables="scripting.state.value?.tabularia_variables ?? []"
      :read-only="readOnly"
      :last-run="last"
      @update="emit('update', $event)"
      @close="editorOpen = false"
    />
  </div>
</template>

<style scoped>
.script-panel { display: flex; flex-direction: column; gap: 8px; }
.row { display: flex; align-items: center; gap: 8px; }
.row h3 { flex: 1; margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 14px; }
.del { border: 0; background: none; color: var(--muted); cursor: pointer; padding: 4px; border-radius: 6px; }
.del:hover { color: var(--danger); background: var(--bg-soft); }
label { font-size: 12px; color: var(--muted); }
.outhint { margin: 0; font-size: 12px; line-height: 1.5; }
.warn { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12px; color: var(--warning); }
.code { display: flex; flex-direction: column; gap: 6px; padding: 10px; border: 1px solid var(--border-soft); border-radius: 8px; }
.code-files { font-size: 12px; overflow-wrap: anywhere; }
.code button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
.trial { display: flex; flex-direction: column; gap: 6px; }
.trial button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
.esito { margin: 0; display: flex; gap: 6px; align-items: flex-start; flex-wrap: wrap; font-size: 12px; }
.esito.ok svg { color: var(--accent-2); }
.esito.ko, .esito.ko svg { color: var(--danger); }
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.log { margin: 6px 0 0; max-height: 220px; overflow: auto; font-size: 11px; padding: 6px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.numbers { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.numbers label { display: flex; flex-direction: column; gap: 4px; }
.numbers input { min-width: 0; }
</style>
