<script setup lang="ts">
// The form of a Predict node (`type: model_predict`): the model of an MLflow server that the
// rows reaching the node are given the predictions of. The MLflow connection, the experiment
// (read from the server), then the version: one that Tabularia registered from that experiment,
// or «latest version» of a registered model, which is read again at each run. Below, the options
// the chosen model has (an interval, a threshold, a horizon, the times to give the survival at).
// The lists come from the gateway's browse routes (composables/useModelGroups.ts); with no
// answer the experiment is written.
//
// Every control is a native one or the app's Select: Tab, Space, Enter and the arrows; no key of
// this form needs F, Home, End or Menu.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Info, TriangleAlert } from 'lucide-vue-next'
import type { ColumnInfo } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useModelGroups, type MlflowExperiment, type MlflowVersion } from '~/composables/useModelGroups'
import { vDraft } from '~/composables/draftField'
import {
  MODEL_RETRIES_MAX, MODEL_TIMEOUT_MIN, versionHeld, MODEL_TIMEOUT_SECONDS, modelNumber, modelNumberProblem, modelNumbersProblem, predictColumns,
  predictFields, predictParams, type ModelField, type ModelOff,
} from '~/composables/modelCatalog'

const props = defineProps<{
  nodeId: string
  // the node's data: connectionId, experiment, registeredModel, version, params, timeoutSeconds, retries
  data: Record<string, any>
  inputColumns: ColumnInfo[]
  connections?: { id: number; name: string; db_type: string }[]
  off?: ModelOff
  maxTimeout?: number | null
  readOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'update', patch: Record<string, any>): void }>()
const { t, te } = useI18n()
const fmt = useFormat()
const models = useModelGroups()

const idOf = (key: string, part = '') => `predict-${props.nodeId}-${key}${part ? `-${part}` : ''}`
const params = computed<Record<string, any>>(() => props.data?.params ?? {})

// ── the connection, the experiment, the version ──────────────────────────────
const connections = computed(() => (props.connections ?? []).filter((c) => c.db_type === 'mlflow'))
const connectionOptions = computed(() => {
  const id = props.data?.connectionId
  const options = connections.value.map((c) => ({ value: c.id, label: c.name }))
  return typeof id === 'number' && !options.some((o) => o.value === id)
    ? [...options, { value: id, label: t('models.mlflow.unknownConnection', { id }) }]
    : options
})

const experiments = ref<MlflowExperiment[]>([])
const experimentsState = ref<'idle' | 'loading' | 'done' | 'failed'>('idle')
const versions = ref<MlflowVersion[]>([])
const versionsState = ref<'idle' | 'loading' | 'done' | 'failed'>('idle')
// only the last question of each list writes its answer; one counter apiece, or the second
// watcher (immediate, as the first) would cancel the first's answer and leave its skeleton on
let askedExperiments = 0
let askedVersions = 0

watch(() => props.data?.connectionId, async (id) => {
  const seq = ++askedExperiments
  experiments.value = []
  if (typeof id !== 'number') { experimentsState.value = 'idle'; return }
  experimentsState.value = 'loading'
  try {
    const list = await models.experiments(id)
    if (seq === askedExperiments) { experiments.value = list; experimentsState.value = 'done' }
  } catch {
    if (seq === askedExperiments) experimentsState.value = 'failed'
  }
}, { immediate: true })

watch(() => [props.data?.connectionId, props.data?.experiment] as const, async ([id, experiment]) => {
  const seq = ++askedVersions
  versions.value = []
  if (typeof id !== 'number' || typeof experiment !== 'string' || !experiment) { versionsState.value = 'idle'; return }
  versionsState.value = 'loading'
  try {
    const list = await models.versions(id, experiment)
    if (seq === askedVersions) { versions.value = list; versionsState.value = 'done' }
  } catch {
    if (seq === askedVersions) versionsState.value = 'failed'
  }
}, { immediate: true })

function pickConnection(id: unknown) {
  // another server: nothing of the experiment and the version holds
  emit('update', { connectionId: typeof id === 'number' ? id : null, experiment: '', registeredModel: '', version: '', params: {} })
}
function pickExperiment(name: unknown) {
  emit('update', { experiment: typeof name === 'string' ? name : '', registeredModel: '', version: '', params: {} })
}
const experimentOptions = computed(() => {
  const current = props.data?.experiment
  const options = experiments.value.map((e) => ({ value: e.name, label: e.name }))
  return current && !options.some((o) => o.value === current) ? [{ value: current, label: current }, ...options] : options
})

// a version is chosen by its model and number: «latest» is a version too, read at each run
const LATEST = 'latest'
const keyOf = (model: string, version: string) => `${model}\u0001${version}`
const versionOptions = computed(() => {
  const options: { value: string; label: string; hint?: string; group?: string }[] = []
  const seen = new Set<string>()
  for (const v of versions.value) {
    if (seen.has(v.model)) continue
    seen.add(v.model)
    options.push({ value: keyOf(v.model, LATEST), label: t('models.predict.latest', { model: v.model }), group: v.model })
  }
  for (const v of versions.value) {
    const parts = [
      v.created_at ? fmt.dateTime(v.created_at) : '',
      v.flow ? `${v.flow}${v.node ? ` · ${v.node}` : ''}` : '',
      v.metric ? `${v.metric.name} ${Number(v.metric.value.toPrecision(4))}` : '',
    ].filter(Boolean)
    options.push({ value: keyOf(v.model, v.version), label: t('models.predict.version', { model: v.model, version: v.version }), hint: parts.join(' · '), group: v.model })
  }
  // the version the node holds stays listed when the server no longer says it
  const model = props.data?.registeredModel
  const version = props.data?.version
  if (model && version && !options.some((o) => o.value === keyOf(model, version))) {
    options.unshift({ value: keyOf(model, version), label: version === LATEST ? t('models.predict.latest', { model }) : t('models.predict.version', { model, version }), hint: t('models.predict.notListed') })
  }
  return options
})
const chosenVersion = computed(() => (props.data?.registeredModel && props.data?.version ? keyOf(props.data.registeredModel, props.data.version) : null))
function pickVersion(value: unknown) {
  if (typeof value !== 'string') return
  const [registeredModel, version] = value.split('\u0001')
  emit('update', { registeredModel, version, params: {} })
}
// the model the version holds decides which options there are
// (the version the node holds, or the newest one for «latest»: not the first the server lists)
const held = computed(() => versionHeld(versions.value, props.data?.registeredModel, props.data?.version))
const kind = computed(() => held.value?.kind ?? versions.value.find((v) => v.model === props.data?.registeredModel)?.kind ?? null)
// the variant of the version (the tag tabularia.variant): the boosting models' options depend on it
const variant = computed(() => held.value?.variant ?? null)

// ── the options of that model ────────────────────────────────────────────────
const fields = computed<ModelField[]>(() => predictFields(kind.value, undefined, variant.value))
const labelOf = (f: ModelField) => (te(`predict.params.${f.key}`) ? t(`predict.params.${f.key}`) : f.key)
const hintOf = (f: ModelField) => (te(`predict.hints.${f.key}`) ? t(`predict.hints.${f.key}`) : '')
function set(key: string, value: unknown) {
  emit('update', { params: predictParams(kind.value, { ...params.value, [key]: value }, undefined, variant.value) })
}
function numberProblem(f: ModelField): string {
  const problem = modelNumberProblem(f, params.value[f.key])
  return problem ? t(`models.form.${problem}`, { above: f.above ?? '', below: f.below ?? '', min: f.min ?? '', max: f.max ?? '' }) : ''
}
function numbersProblem(f: ModelField): string {
  const problem = modelNumbersProblem(f, params.value[f.key])
  return problem ? t(`models.form.${problem}`, { above: f.above ?? '', below: f.below ?? '', min: f.min ?? '', max: f.max ?? '', n: f.max_count ?? '' }) : ''
}
const timesText = (f: ModelField) => (Array.isArray(params.value[f.key]) ? params.value[f.key].join(', ') : '')

// the columns the step adds, and the ones of its input they would overwrite (the form refuses)
const added = computed(() => predictColumns(kind.value, params.value, undefined, variant.value))
const clash = computed(() => added.value.filter((c) => props.inputColumns.some((x) => x.name === c)))

// ── the run's limits ─────────────────────────────────────────────────────────
const timeoutMax = computed(() => props.maxTimeout ?? 86400)
function limit(key: 'timeoutSeconds' | 'retries', raw: string, min: number, max: number) {
  const n = modelNumber(raw)
  if (!Number.isFinite(n)) return
  emit('update', { [key]: Math.min(max, Math.max(min, Math.round(n))) })
}
const offText = computed(() => {
  switch (props.off) {
    case 'kubernetes': return t('scripting.kubernetesOnly')
    case 'not_configured': return t('models.node.notConfigured')
    case 'not_enabled': case 'not_allowed': return t('models.predict.noGroup')
    default: return ''
  }
})
</script>

<template>
  <fieldset class="modelform" :disabled="readOnly">
    <p class="about">{{ $t('models.predict.hint') }}</p>
    <p v-if="offText" class="note warn" role="status"><TriangleAlert :size="13" aria-hidden="true" /> {{ offText }}</p>

    <div class="field">
      <span :id="idOf('connection', 'label')" class="lbl">{{ $t('models.mlflow.connection') }}<span class="req" aria-hidden="true"> *</span></span>
      <Select
        :model-value="data.connectionId ?? null"
        :options="connectionOptions"
        :placeholder="'—'"
        :aria-labelledby="idOf('connection', 'label')"
        @update:model-value="pickConnection"
      />
      <p v-if="!connections.length" class="hint">{{ $t('models.mlflow.noConnection') }}</p>
    </div>

    <div v-if="typeof data.connectionId === 'number'" class="field">
      <span :id="idOf('experiment', 'label')" class="lbl">{{ $t('models.mlflow.experiment') }}<span class="req" aria-hidden="true"> *</span></span>
      <span v-if="experimentsState === 'loading'" class="sk" style="width: 140px" aria-busy="true" />
      <input
        v-else-if="experimentsState === 'failed'"
        type="text"
        autocomplete="off"
        :value="data.experiment ?? ''"
        v-draft="data.experiment ?? ''"
        :aria-labelledby="idOf('experiment', 'label')"
        @change="pickExperiment(($event.target as HTMLInputElement).value.trim())"
      />
      <Select
        v-else
        :model-value="data.experiment || null"
        :options="experimentOptions"
        searchable
        :placeholder="'—'"
        :aria-labelledby="idOf('experiment', 'label')"
        @update:model-value="pickExperiment"
      />
      <p v-if="experimentsState === 'failed'" class="note warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('models.predict.listFailed') }}</p>
      <p v-else-if="experimentsState === 'done' && !experiments.length" class="hint">{{ $t('models.predict.noExperiments') }}</p>
    </div>

    <div v-if="data.experiment" class="field">
      <span :id="idOf('version', 'label')" class="lbl">{{ $t('models.predict.versionLabel') }}<span class="req" aria-hidden="true"> *</span></span>
      <span v-if="versionsState === 'loading'" class="sk" style="width: 140px" aria-busy="true" />
      <Select
        v-else
        :model-value="chosenVersion"
        :options="versionOptions"
        searchable
        :placeholder="'—'"
        :aria-labelledby="idOf('version', 'label')"
        @update:model-value="pickVersion"
      />
      <p v-if="versionsState === 'failed'" class="note warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('models.predict.listFailed') }}</p>
      <p v-else-if="versionsState === 'done' && !versions.length" class="hint">{{ $t('models.predict.noVersions') }}</p>
      <p v-else-if="data.version === 'latest'" class="hint">{{ $t('models.predict.latestHint') }}</p>
    </div>

    <template v-for="f in fields" :key="f.key">
      <div v-if="f.control === 'boolean'" class="field">
        <label class="chk">
          <input type="checkbox" role="switch" :checked="params[f.key] === true" @change="set(f.key, ($event.target as HTMLInputElement).checked)" />
          {{ labelOf(f) }}
        </label>
        <p v-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
      </div>
      <div v-else-if="f.control === 'numbers'" class="field">
        <label :for="idOf(f.key)" class="lbl">{{ labelOf(f) }}</label>
        <input
          :id="idOf(f.key)" type="text" inputmode="decimal" autocomplete="off"
          :value="timesText(f)" v-draft="timesText(f)" :placeholder="$t('models.form.numbersPlaceholder')"
          :aria-invalid="numbersProblem(f) ? 'true' : undefined"
          @change="set(f.key, ($event.target as HTMLInputElement).value)"
        />
        <p v-if="numbersProblem(f)" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ numbersProblem(f) }}</p>
        <p v-else-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
      </div>
      <div v-else class="field">
        <label :for="idOf(f.key)" class="lbl">{{ labelOf(f) }}</label>
        <input
          :id="idOf(f.key)" type="number" step="any"
          :value="params[f.key] ?? ''" v-draft="params[f.key] ?? ''"
          :placeholder="String(f.key === 'threshold' ? 0.5 : '')"
          :aria-invalid="numberProblem(f) ? 'true' : undefined"
          @change="set(f.key, ($event.target as HTMLInputElement).value)"
        />
        <p v-if="numberProblem(f)" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ numberProblem(f) }}</p>
        <p v-else-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
      </div>
    </template>

    <p v-if="added.length" class="note" role="status">
      <Info :size="13" aria-hidden="true" /> {{ $t('models.predict.adds', { columns: added.join(', ') }) }}
    </p>
    <p v-if="clash.length" class="note warn" role="alert">
      <TriangleAlert :size="13" aria-hidden="true" /> {{ $t('models.predict.clash', { columns: clash.join(', ') }) }}
    </p>

    <section class="block" :aria-labelledby="idOf('run', 'title')">
      <h4 :id="idOf('run', 'title')">{{ $t('models.run.title') }}</h4>
      <div class="numbers">
        <label class="field">
          <span class="lbl">{{ $t('models.run.timeout') }}</span>
          <input
            type="number" :min="MODEL_TIMEOUT_MIN" :max="timeoutMax"
            :value="data.timeoutSeconds ?? MODEL_TIMEOUT_SECONDS" v-draft="data.timeoutSeconds ?? MODEL_TIMEOUT_SECONDS"
            @change="limit('timeoutSeconds', ($event.target as HTMLInputElement).value, MODEL_TIMEOUT_MIN, timeoutMax)"
          />
        </label>
        <label class="field">
          <span class="lbl">{{ $t('models.run.retries') }}</span>
          <input
            type="number" min="0" :max="MODEL_RETRIES_MAX"
            :value="data.retries ?? 0" v-draft="data.retries ?? 0"
            @change="limit('retries', ($event.target as HTMLInputElement).value, 0, MODEL_RETRIES_MAX)"
          />
        </label>
      </div>
      <!-- Run on the sample, and what the latest run left (ModelRun, given by the node's panel) -->
      <slot name="run" />
    </section>
  </fieldset>
</template>

<style scoped>
.modelform { display: flex; flex-direction: column; gap: 12px; border: none; margin: 0; padding: 0; min-width: 0; }
.about { margin: 0; font-size: 12px; line-height: 1.45; color: var(--muted); }
.field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.lbl { font-size: 12px; color: var(--muted); padding: 0; }
.req { color: var(--muted); }
.hint { margin: 0; font-size: 11.5px; line-height: 1.4; color: var(--muted); }
.note { margin: 0; font-size: 12px; line-height: 1.4; color: var(--muted); display: flex; align-items: flex-start; gap: 5px; }
.note svg { flex-shrink: 0; margin-top: 2px; }
.note.warn { color: var(--warning); }
.chk { display: flex; align-items: flex-start; gap: 6px; color: var(--text); font-size: 13px; }
.chk input { width: auto; flex-shrink: 0; margin-top: 2px; }
.block { display: flex; flex-direction: column; gap: 10px; padding-top: 10px; border-top: 1px solid var(--border-soft); }
.block h4 { margin: 0; font-size: 12px; font-weight: 600; color: var(--text); }
.numbers { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.numbers input { min-width: 0; }
</style>
