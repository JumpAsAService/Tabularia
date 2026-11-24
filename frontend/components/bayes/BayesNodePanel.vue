<script setup lang="ts">
// The node panel of a Bayesian model node (docs/specs/bayes-flow-node.md, «The panel»): the saved model and the version it pins
// (with the notice «a newer version exists» and its «Use version N»), one row for each data variable of the pinned version with
// the column it reads, the sampler's options («Advanced» holds adapt_delta, max_treedepth and the initial values), the output,
// the limits, and «Run» (ModelRun.vue, as a Model node's). The binding happens only here (the owner's answer 6); the gateway
// checks it again at the run. The node never holds the program: it holds the model's id, the version and the choices.
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ExternalLink, RotateCcw, Shuffle, Sigma, Trash2, TriangleAlert } from 'lucide-vue-next'
import type { Node } from '@vue-flow/core'
import type { ColumnInfo } from '~/composables/useApi'
import type { ScriptOutputs } from '~/composables/useScripting'
import type { BayesBinding } from '~/composables/bayesGraph'
import { modelOff } from '~/composables/modelCatalog'
import {
  BAYES_NODE_GROUP_DEFAULTS, BAYES_NODE_OUTPUTS, BAYES_NODE_RETRIES_MAX, BAYES_NODE_SAMPLER, BAYES_NODE_TIMEOUT,
  bayesNodeChecks, bayesNodeColumnsFor, bayesNodeDelta, bayesNodeInitKeep, bayesNodeInitMode, bayesNodeInitTargets, bayesNodeInitWith,
  bayesNodeMove, bayesNodeNameOf, bayesNodeNewer, bayesNodeNotice, bayesNodeRead, bayesNodeReplicated, bayesNodeSamplerOf, bayesNodeSeed,
  bayesNodeBindable, bayesNodeUnbound, bayesNodeVariables, bayesNodeWhole,
  type BayesNodeInitMode,
} from '~/composables/bayesNode'
import { useBayesModels, bayesFamilyKey, type BayesModelVersion } from '~/composables/useBayesModels'
import { useBayesPins } from '~/composables/useBayesPins'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useFormat } from '~/composables/useFormat'
import { useModelGroups } from '~/composables/useModelGroups'
import { vDraft } from '~/composables/draftField'
import Select from '~/components/ui/Select.vue'
import ModelRun from '~/components/models/ModelRun.vue'

const props = defineProps<{
  node: Node
  inputColumns: ColumnInfo[]
  columnsLoading?: boolean
  readOnly?: boolean
  // the node's latest trial or run and the trial in progress (the editor's, as a Script node's)
  outputs?: ScriptOutputs[string] | null
  trial?: { runId: number | null; status: string; startedAt: number; error: string } | null
  flowDirty?: boolean
}>()
const emit = defineEmits<{
  (e: 'update', patch: Record<string, any>): void
  (e: 'delete'): void
  (e: 'run'): void
}>()
const { t } = useI18n()
const fmt = useFormat()
const api = useBayesModels()
const pins = useBayesPins()
const folderPaths = useFolderPaths()
const groups = useModelGroups()
onMounted(() => { pins.load(); groups.load() })

const data = computed<Record<string, any>>(() => props.node.data ?? {})
const modelId = computed(() => (Number.isInteger(data.value.bayesModelId) ? (data.value.bayesModelId as number) : null))
const pinned = computed(() => (Number.isInteger(data.value.bayesModelVersion) ? (data.value.bayesModelVersion as number) : null))
const bindings = computed<Record<string, string>>(() => (data.value.bindings && typeof data.value.bindings === 'object' ? data.value.bindings : {}))
const sampler = computed(() => bayesNodeSamplerOf(data.value.sampler))
const output = computed(() => (BAYES_NODE_OUTPUTS as readonly string[]).includes(data.value.output) ? (data.value.output as string) : 'summary')
const columns = computed(() => props.inputColumns ?? [])

// ── the group ───────────────────────────────────────────────────────────────
const off = computed(() => modelOff('bayes', groups.state.value))
const groupState = computed(() => groups.state.value?.groups.find((g) => g.id === 'bayes') ?? null)
const groupWhy = computed(() => {
  switch (off.value) {
    case 'kubernetes': return t('scripting.kubernetesOnly')
    case 'not_configured': return t('models.node.notConfigured')
    case 'not_enabled': return t('models.node.groupOff', { group: t('models.groups.bayes') })
    case 'not_allowed': return t('models.node.groupNotAllowed')
    default: return ''
  }
})
const maxTimeout = computed(() => groupState.value?.max_timeout_seconds ?? BAYES_NODE_GROUP_DEFAULTS.max_seconds)
const maxRows = computed(() => groupState.value?.max_rows ?? BAYES_NODE_GROUP_DEFAULTS.max_rows)

// ── the model and its version ───────────────────────────────────────────────
watch(modelId, (id) => { if (id != null) pins.loadVersions(id) }, { immediate: true })
const listed = computed(() => (modelId.value == null ? null : (pins.models.value?.find((m) => m.id === modelId.value) ?? null)))
// the list is read and does not hold the model: this person does not see it (or it is gone: the gateway says the same)
const unavailable = computed(() => modelId.value != null && !!pins.models.value && !listed.value)
const versionList = computed(() => (modelId.value == null ? [] : (pins.versionsOf.value[modelId.value] ?? [])))
const pinnedEntry = computed(() => versionList.value.find((v) => v.version === pinned.value) ?? null)
const newer = computed(() => (listed.value && pinned.value != null ? bayesNodeNewer(listed.value.current_version, pinned.value, versionList.value) : null))
const notice = computed(() => bayesNodeNotice(newer.value))

const options = computed(() => (pins.models.value ?? []).map((m) => ({
  value: m.id,
  label: m.name,
  hint: [folderPaths.pathOf(m.project_id), t(bayesFamilyKey(m.family))].filter(Boolean).join(' · '),
})))
function chooseModel(id: number | null) {
  const m = pins.models.value?.find((x) => x.id === id)
  if (!m || m.id === modelId.value) return
  moved.value = null
  // the model and its current version together; the bindings and the initial values are the old model's: not carried over
  emit('update', { bayesModelId: m.id, bayesModelVersion: m.current_version, bindings: {}, sampler: { ...sampler.value, init: null } })
}

// the pinned version in full: its variables, its manifests
const detail = ref<BayesModelVersion | null>(null)
const detailError = ref('')
let ask = 0
watch([modelId, pinned], async ([id, v]) => {
  const mine = ++ask
  detail.value = null
  detailError.value = ''
  if (id == null || v == null) return
  try {
    const got = await api.version(id, v)
    if (mine === ask) detail.value = got
  } catch (e: any) {
    if (mine === ask) detailError.value = String(e?.data?.detail ?? e?.message ?? e)
  }
}, { immediate: true })

const read = computed(() => (detail.value ? bayesNodeRead(detail.value.graph) : null))
const variables = computed<BayesBinding[]>(() => bayesNodeBindable(read.value?.bindings ?? []))
const unbound = computed(() => bayesNodeUnbound(variables.value, bindings.value))
const checks = computed(() => (detail.value ? bayesNodeChecks(detail.value.data_manifest) : null))
const initTargets = computed(() => (detail.value && read.value ? bayesNodeInitTargets(detail.value.quantity_manifest, read.value.params.keys()) : []))
const savedLine = computed(() => {
  const e = pinnedEntry.value
  if (pinned.value == null) return ''
  if (!e) return `v${pinned.value}`
  return e.created_by
    ? t('bayesNode.model.savedBy', { version: e.version, by: e.created_by, date: fmt.date(e.created_at) })
    : t('bayesNode.model.savedOn', { version: e.version, date: fmt.date(e.created_at) })
})
// the compile of the model's current version (the one its list holds): said for the pinned one only when it is the current one
const compileWords = computed(() => (listed.value && pinned.value === listed.value.current_version ? t(`bayesModels.compile_${listed.value.compile.state}`) : ''))

// «Use version N»: the pin moves to the current version; the bindings of the data nodes still there stay when they still fit
const moved = ref<null | { version: number; kept: number; cleared: string[]; unbound: string[] }>(null)
const moving = ref(false)
const moveError = ref('')
watch(() => props.node.id, () => { moved.value = null; moveError.value = '' })
async function useNewer() {
  if (modelId.value == null || moving.value) return
  moving.value = true
  moveError.value = ''
  try {
    const v = await api.version(modelId.value, 0)
    const next = bayesNodeVariables(v.graph)
    const move = bayesNodeMove(bindings.value, next, columns.value)
    const model = bayesNodeRead(v.graph)
    const targets = model ? bayesNodeInitTargets(v.quantity_manifest, model.params.keys()) : []
    moved.value = { version: v.version, kept: move.kept.length, cleared: move.cleared.map(bayesNodeNameOf), unbound: move.unbound.map(bayesNodeNameOf) }
    emit('update', {
      bayesModelId: modelId.value, bayesModelVersion: v.version, bindings: move.bindings,
      sampler: { ...sampler.value, init: bayesNodeInitKeep(sampler.value.init, targets) },
    })
    pins.load(true)
    pins.loadVersions(modelId.value, true)
  } catch (e: any) {
    moveError.value = String(e?.data?.detail ?? e?.message ?? e)
  } finally {
    moving.value = false
  }
}

// ── the columns ─────────────────────────────────────────────────────────────
const typeWords = (v: BayesBinding): string => {
  if (v.type === 'int') return t(`bayesNode.type.int_${v.support === 'binary' || v.support === 'nonnegative' ? v.support : 'any'}`)
  return t(`bayesNode.type.${v.type}`)
}
function columnOptions(v: BayesBinding) {
  const fits = bayesNodeColumnsFor(v, columns.value).map((c) => ({ value: c.name, label: c.name }))
  const bound = bindings.value[v.node]
  // a column the input no longer has stays in the list, said so: the person sees what the node still reads
  const gone = bound && columns.value.length > 0 && !columns.value.some((c) => c.name === bound) ? [{ value: bound, label: bound, hint: t('bayesNode.variables.gone') }] : []
  // a column the input has but the variable does not take any more (it stays bound until the person chooses another)
  const misfit = bound && columns.value.some((c) => c.name === bound) && !fits.some((o) => o.value === bound) ? [{ value: bound, label: bound, hint: t('bayesNode.variables.misfit') }] : []
  return [...(bound ? [{ value: '', label: t('bayesNode.variables.unbind') }] : []), ...gone, ...misfit, ...fits]
}
function pickColumn(node: string, column: string | null) {
  const next = { ...bindings.value }
  if (column) next[node] = column
  else delete next[node]
  emit('update', { bindings: next })
}
// said only when the input's columns are known: with none read (an input not set, a preview not back) nothing is claimed gone
const missing = (v: BayesBinding) => { const c = bindings.value[v.node]; return !!c && columns.value.length > 0 && !columns.value.some((x) => x.name === c) && !props.columnsLoading }

// ── the sampler ─────────────────────────────────────────────────────────────
const S = BAYES_NODE_SAMPLER
function patchSampler(patch: Record<string, any>) {
  emit('update', { sampler: { ...sampler.value, ...patch } })
}
const MAIN_FIELDS = [
  { key: 'chains', label: 'bayesNode.sampler.chains', ...S.chains },
  { key: 'warmup', label: 'bayesNode.sampler.warmup', ...S.warmup },
  { key: 'draws', label: 'bayesNode.sampler.draws', ...S.draws },
] as const
function setWhole(key: 'chains' | 'warmup' | 'draws' | 'max_treedepth', text: string) {
  const f = S[key]
  patchSampler({ [key]: bayesNodeWhole(text, f.min, f.max, sampler.value[key]) })
}
const drawAnother = () => patchSampler({ seed: bayesNodeSeed() })
function setSeed(text: string) {
  const n = bayesNodeWhole(text, 1, 4_294_967_295, sampler.value.seed)
  patchSampler({ seed: n })
}
const setDelta = (text: string) => patchSampler({ adapt_delta: bayesNodeDelta(text, sampler.value.adapt_delta) })

// initial values: CmdStan's own, a radius, or a value for each single-number parameter
const initMode = ref<BayesNodeInitMode>(bayesNodeInitMode(sampler.value.init))
watch(() => props.node.id, () => { initMode.value = bayesNodeInitMode(sampler.value.init) })
function setInitMode(mode: BayesNodeInitMode) {
  initMode.value = mode
  if (mode === 'default') patchSampler({ init: null })
  else if (mode === 'radius') patchSampler({ init: typeof sampler.value.init === 'number' ? sampler.value.init : 2 })
  else patchSampler({ init: sampler.value.init && typeof sampler.value.init === 'object' ? sampler.value.init : null })
}
const setRadius = (text: string) => {
  const x = Number(text)
  patchSampler({ init: text.trim() !== '' && Number.isFinite(x) && x >= 0 ? x : 2 })
}
const initValue = (stan: string): string => { const i = sampler.value.init; return i && typeof i === 'object' && stan in i ? String(i[stan]) : '' }
const setInitValue = (stan: string, text: string) => patchSampler({ init: bayesNodeInitWith(sampler.value.init, stan, text) })
const labelOfNode = (id: string) => { const n = read.value?.graph.nodes.find((x) => x.id === id); return n ? (n.label || n.name || id) : id }

// ── the output and the limits ───────────────────────────────────────────────
const replicated = computed(() => bayesNodeReplicated(data.value.replicatedDraws, sampler.value.draws))
const setReplicated = (text: string) => emit('update', { replicatedDraws: bayesNodeWhole(text, 1, Math.max(1, sampler.value.draws), replicated.value) })
function numberField(key: string, text: string, min: number, max: number, fallback: number) {
  emit('update', { [key]: bayesNodeWhole(text, min, max, fallback) })
}

// a node made before this panel (or by hand) lacks its seed or its timeout: the gateway refuses a node with no seed, and a missing
// timeout is 900 s, short for sampling: the panel writes them once, when it opens the node
watch(() => props.node.id, () => {
  if (props.readOnly) return
  const patch: Record<string, any> = {}
  if (!(typeof data.value.sampler?.seed === 'number' && data.value.sampler.seed >= 1)) patch.sampler = { ...sampler.value, seed: bayesNodeSeed() }
  if (typeof data.value.timeoutSeconds !== 'number') patch.timeoutSeconds = BAYES_NODE_TIMEOUT.default
  if (Object.keys(patch).length) emit('update', patch)
}, { immediate: true })

// ── Run ─────────────────────────────────────────────────────────────────────
const why = computed(() => {
  if (props.readOnly) return ''
  if (modelId.value == null) return t('bayesNode.run.chooseModel')
  if (unavailable.value) return t('bayesNode.model.unavailableRun')
  if (!detail.value) return detailError.value ? '' : t('bayesNode.run.reading')
  if (unbound.value.length) return t('bayesNode.run.bindFirst', { names: unbound.value.map(bayesNodeNameOf).join(', ') })
  return ''
})
// a version that could not be read cannot run either: its variables are not known
const disabled = computed(() => !!off.value || !!why.value || (modelId.value != null && !detail.value))
const title = computed(() => (data.value.label as string) || t('models.bayes_model.name'))
</script>

<template>
  <div class="bayes-panel">
    <div class="row">
      <h3><Sigma :size="15" aria-hidden="true" /> {{ title }}</h3>
      <button v-if="!readOnly" class="del" :title="$t('nodePanel.deleteNodeTitle')" :aria-label="$t('nodePanel.deleteNodeTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
    </div>
    <p v-if="groupWhy" class="note warn" role="note"><TriangleAlert :size="13" aria-hidden="true" /> {{ groupWhy }}</p>

    <!-- 1. the model and its version -->
    <div class="block first">
      <label class="lbl" :for="`bn-model-${node.id}`">{{ $t('bayesNode.model.label') }}</label>
      <Select
        :id="`bn-model-${node.id}`"
        :model-value="modelId"
        :options="options"
        :placeholder="$t('bayesNode.model.choose')"
        :disabled="readOnly"
        searchable
        @update:model-value="chooseModel"
      />
      <p v-if="pins.models.value && !pins.models.value.length" class="hint">{{ $t('bayesNode.model.none') }}</p>
      <p v-if="pins.failed.value && !pins.models.value" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('bayesNode.model.listFailed') }}</p>
      <p v-if="unavailable" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('bayesNode.model.unavailable') }}</p>
      <template v-else-if="modelId != null">
        <p class="hint version">{{ savedLine }}<template v-if="compileWords"> · {{ compileWords }}</template></p>
        <NuxtLink v-if="listed" :to="`/bayes-model?id=${modelId}`" target="_blank" class="btnlink small"><ExternalLink :size="12" aria-hidden="true" /> {{ $t('bayesNode.model.open') }}</NuxtLink>
      </template>

      <!-- 2. the notice -->
      <div v-if="notice && newer && !readOnly" class="notice" role="status">
        <p><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('bayesNode.model.newer', { version: newer.version }) }}</p>
        <button type="button" :disabled="moving" @click="useNewer">{{ $t('bayesNode.model.useVersion', { version: newer.version }) }}</button>
      </div>
      <p v-else-if="notice && newer" class="note"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('bayesNode.model.newer', { version: newer.version }) }}</p>
      <p v-if="moveError" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ moveError }}</p>
      <div v-if="moved" class="moved" role="status">
        <p>{{ $t('bayesNode.model.moved', { version: moved.version, n: moved.kept }) }}</p>
        <p v-if="moved.cleared.length">{{ $t('bayesNode.model.movedCleared', { names: moved.cleared.join(', ') }) }}</p>
        <p v-if="moved.unbound.length">{{ $t('bayesNode.model.movedUnbound', { names: moved.unbound.join(', ') }) }}</p>
      </div>
    </div>

    <!-- 3. the variables -->
    <div v-if="modelId != null && !unavailable" class="block">
      <h4>{{ $t('bayesNode.variables.title') }}</h4>
      <p v-if="detailError" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('bayesNode.model.readFailed', { error: detailError }) }}</p>
      <p v-else-if="!detail" class="hint">{{ $t('bayesNode.run.reading') }}</p>
      <template v-else>
        <p class="hint">{{ $t('bayesNode.variables.hint') }}</p>
        <p v-if="!variables.length" class="hint">{{ $t('bayesNode.variables.none') }}</p>
        <ul v-else class="vars">
          <li v-for="v in variables" :key="v.node" class="var">
            <label class="vname" :for="`bn-var-${node.id}-${v.node}`">
              <span class="vlabel">{{ bayesNodeNameOf(v) }}</span>
              <code v-if="v.name && v.label && v.name !== v.label" class="vcode">{{ v.name }}</code>
            </label>
            <span class="vtype">{{ typeWords(v) }}</span>
            <span v-if="v.standardize" class="tag">{{ $t('bayesNode.variables.standardized') }}</span>
            <span v-if="v.observed" class="tag">{{ $t('bayesNode.variables.outcome') }}</span>
            <Select
              :id="`bn-var-${node.id}-${v.node}`"
              :model-value="bindings[v.node] ?? null"
              :options="columnOptions(v)"
              :placeholder="columnsLoading ? $t('bayesNode.variables.loading') : $t('bayesNode.variables.choose')"
              :disabled="readOnly"
              @update:model-value="pickColumn(v.node, $event)"
            />
            <p v-if="missing(v)" class="note warn"><TriangleAlert :size="12" aria-hidden="true" /> {{ $t('bayesNode.variables.goneNamed', { column: bindings[v.node] }) }}</p>
            <p v-else-if="!bindings[v.node] && !columnsLoading && !bayesNodeColumnsFor(v, columns).length" class="hint">{{ $t('bayesNode.variables.noneFit') }}</p>
          </li>
        </ul>
        <p v-if="checks" class="hint">{{ $t(`bayesNode.checks.${checks}`) }}</p>
      </template>
    </div>

    <!-- 4. the sampler -->
    <div class="block">
      <h4>{{ $t('bayesNode.sampler.title') }}</h4>
      <p class="hint">{{ $t('bayesNode.sampler.nuts') }}</p>
      <div class="numbers">
        <div v-for="f in MAIN_FIELDS" :key="f.key" class="nfield">
          <label :for="`bn-${f.key}-${node.id}`">{{ $t(f.label) }}</label>
          <div class="nrow">
            <input :id="`bn-${f.key}-${node.id}`" type="number" inputmode="numeric" :min="f.min" :max="f.max" :value="sampler[f.key]" v-draft="sampler[f.key]" :disabled="readOnly"
                   @change="setWhole(f.key, ($event.target as HTMLInputElement).value)" />
            <button v-if="!readOnly && sampler[f.key] !== f.default" type="button" class="icon" :title="$t('bayesNode.sampler.reset')" :aria-label="$t('bayesNode.sampler.resetField', { name: $t(f.label) })" @click="patchSampler({ [f.key]: f.default })"><RotateCcw :size="12" aria-hidden="true" /></button>
          </div>
          <span class="def">{{ $t('bayesNode.sampler.default', { value: fmt.count(f.default) }) }}</span>
        </div>
        <div class="nfield">
          <label :for="`bn-seed-${node.id}`">{{ $t('bayesNode.sampler.seed') }}</label>
          <div class="nrow">
            <input :id="`bn-seed-${node.id}`" type="number" inputmode="numeric" min="1" :value="sampler.seed" v-draft="sampler.seed" :disabled="readOnly"
                   @change="setSeed(($event.target as HTMLInputElement).value)" />
            <button v-if="!readOnly" type="button" class="icon" :title="$t('bayesNode.sampler.drawAnother')" :aria-label="$t('bayesNode.sampler.drawAnother')" @click="drawAnother"><Shuffle :size="12" aria-hidden="true" /></button>
          </div>
          <span class="def">{{ $t('bayesNode.sampler.seedHint') }}</span>
        </div>
      </div>

      <details class="advanced">
        <summary>{{ $t('bayesNode.sampler.advanced') }}</summary>
        <div class="numbers">
          <div class="nfield">
            <label :for="`bn-delta-${node.id}`">adapt_delta</label>
            <div class="nrow">
              <input :id="`bn-delta-${node.id}`" type="number" inputmode="decimal" min="0.001" max="0.999" step="0.01" :value="sampler.adapt_delta" v-draft="sampler.adapt_delta" :disabled="readOnly"
                     @change="setDelta(($event.target as HTMLInputElement).value)" />
              <button v-if="!readOnly && sampler.adapt_delta !== S.adapt_delta.default" type="button" class="icon" :title="$t('bayesNode.sampler.reset')" :aria-label="$t('bayesNode.sampler.resetField', { name: 'adapt_delta' })" @click="patchSampler({ adapt_delta: S.adapt_delta.default })"><RotateCcw :size="12" aria-hidden="true" /></button>
            </div>
            <span class="def">{{ $t('bayesNode.sampler.deltaHint', { value: S.adapt_delta.default }) }}</span>
          </div>
          <div class="nfield">
            <label :for="`bn-depth-${node.id}`">max_treedepth</label>
            <div class="nrow">
              <input :id="`bn-depth-${node.id}`" type="number" inputmode="numeric" :min="S.max_treedepth.min" :max="S.max_treedepth.max" :value="sampler.max_treedepth" v-draft="sampler.max_treedepth" :disabled="readOnly"
                     @change="setWhole('max_treedepth', ($event.target as HTMLInputElement).value)" />
              <button v-if="!readOnly && sampler.max_treedepth !== S.max_treedepth.default" type="button" class="icon" :title="$t('bayesNode.sampler.reset')" :aria-label="$t('bayesNode.sampler.resetField', { name: 'max_treedepth' })" @click="patchSampler({ max_treedepth: S.max_treedepth.default })"><RotateCcw :size="12" aria-hidden="true" /></button>
            </div>
            <span class="def">{{ $t('bayesNode.sampler.default', { value: S.max_treedepth.default }) }}</span>
          </div>
        </div>
        <fieldset class="field" :disabled="readOnly">
          <legend class="lbl">{{ $t('bayesNode.sampler.init') }}</legend>
          <label class="chk"><input type="radio" :name="`bn-init-${node.id}`" value="default" :checked="initMode === 'default'" @change="setInitMode('default')" /> {{ $t('bayesNode.sampler.initDefault') }}</label>
          <label class="chk"><input type="radio" :name="`bn-init-${node.id}`" value="radius" :checked="initMode === 'radius'" @change="setInitMode('radius')" /> {{ $t('bayesNode.sampler.initRadius') }}</label>
          <div v-if="initMode === 'radius'" class="nfield indent">
            <label :for="`bn-radius-${node.id}`">{{ $t('bayesNode.sampler.radius') }}</label>
            <input :id="`bn-radius-${node.id}`" type="number" inputmode="decimal" min="0" step="0.5" :value="typeof sampler.init === 'number' ? sampler.init : 2" v-draft="typeof sampler.init === 'number' ? sampler.init : 2"
                   @change="setRadius(($event.target as HTMLInputElement).value)" />
            <span class="def">{{ $t('bayesNode.sampler.radiusHint') }}</span>
          </div>
          <label class="chk"><input type="radio" :name="`bn-init-${node.id}`" value="values" :checked="initMode === 'values'" @change="setInitMode('values')" /> {{ $t('bayesNode.sampler.initValues') }}</label>
          <div v-if="initMode === 'values'" class="indent">
            <p class="hint">{{ $t('bayesNode.sampler.valuesHint') }}</p>
            <p v-if="!initTargets.length" class="hint">{{ $t('bayesNode.sampler.noParameters') }}</p>
            <div v-for="p in initTargets" :key="p.stan" class="nfield">
              <label :for="`bn-init-${node.id}-${p.stan}`">{{ labelOfNode(p.node) }} <code class="vcode">{{ p.stan }}</code></label>
              <input :id="`bn-init-${node.id}-${p.stan}`" type="number" inputmode="decimal" step="any" :value="initValue(p.stan)" v-draft="initValue(p.stan)" :placeholder="$t('bayesNode.sampler.drawn')"
                     @change="setInitValue(p.stan, ($event.target as HTMLInputElement).value)" />
            </div>
          </div>
        </fieldset>
      </details>
    </div>

    <!-- 5. the output -->
    <fieldset class="block field" :disabled="readOnly">
      <legend class="lbl bhead">{{ $t('bayesNode.output.title') }}</legend>
      <label v-for="o in BAYES_NODE_OUTPUTS" :key="o" class="chk opt">
        <input type="radio" :name="`bn-output-${node.id}`" :value="o" :checked="output === o" @change="emit('update', { output: o })" />
        <span><span class="oname">{{ $t(`bayesNode.output.${o}`) }}</span><span class="odesc">{{ $t(`bayesNode.output.${o}Hint`) }}</span></span>
      </label>
      <div v-if="output === 'replicated'" class="nfield indent">
        <label :for="`bn-rep-${node.id}`">{{ $t('bayesNode.output.replicatedDraws') }}</label>
        <input :id="`bn-rep-${node.id}`" type="number" inputmode="numeric" min="1" :max="Math.max(1, sampler.draws)" :value="replicated" v-draft="replicated"
               @change="setReplicated(($event.target as HTMLInputElement).value)" />
        <span class="def">{{ $t('bayesNode.output.replicatedMax', { max: fmt.count(Math.max(1, sampler.draws)) }) }}</span>
      </div>
    </fieldset>

    <!-- 6. the limits -->
    <div class="block">
      <h4>{{ $t('bayesNode.limits.title') }}</h4>
      <div class="numbers three">
        <div class="nfield">
          <label :for="`bn-timeout-${node.id}`">{{ $t('bayesNode.limits.timeout') }}</label>
          <input :id="`bn-timeout-${node.id}`" type="number" inputmode="numeric" :min="BAYES_NODE_TIMEOUT.min" :max="maxTimeout" :value="data.timeoutSeconds ?? BAYES_NODE_TIMEOUT.default" v-draft="data.timeoutSeconds ?? BAYES_NODE_TIMEOUT.default" :disabled="readOnly"
                 @change="numberField('timeoutSeconds', ($event.target as HTMLInputElement).value, BAYES_NODE_TIMEOUT.min, maxTimeout, BAYES_NODE_TIMEOUT.default)" />
          <span class="def">{{ $t('bayesNode.limits.timeoutHint', { value: fmt.count(BAYES_NODE_TIMEOUT.default), max: fmt.count(maxTimeout) }) }}</span>
        </div>
        <div class="nfield">
          <label :for="`bn-retries-${node.id}`">{{ $t('bayesNode.limits.retries') }}</label>
          <input :id="`bn-retries-${node.id}`" type="number" inputmode="numeric" min="0" :max="BAYES_NODE_RETRIES_MAX" :value="data.retries ?? 0" v-draft="data.retries ?? 0" :disabled="readOnly"
                 @change="numberField('retries', ($event.target as HTMLInputElement).value, 0, BAYES_NODE_RETRIES_MAX, 0)" />
        </div>
        <div class="nfield">
          <label :for="`bn-delay-${node.id}`">{{ $t('bayesNode.limits.retryDelay') }}</label>
          <input :id="`bn-delay-${node.id}`" type="number" inputmode="numeric" min="0" max="3600" :value="data.retryDelaySeconds ?? 60" v-draft="data.retryDelaySeconds ?? 60" :disabled="readOnly || !(data.retries > 0)"
                 @change="numberField('retryDelaySeconds', ($event.target as HTMLInputElement).value, 0, 3600, 60)" />
        </div>
      </div>
      <div class="group-limits">
        <p class="hint">{{ $t('bayesNode.limits.group') }}</p>
        <dl>
          <div><dt>{{ $t('bayesNode.limits.rows') }}</dt><dd>{{ fmt.count(maxRows) }}</dd></div>
          <div><dt>{{ $t('bayesNode.limits.quantities') }}</dt><dd>{{ fmt.count(BAYES_NODE_GROUP_DEFAULTS.max_quantities) }}</dd></div>
          <div><dt>{{ $t('bayesNode.limits.units') }}</dt><dd>{{ fmt.count(BAYES_NODE_GROUP_DEFAULTS.max_units) }}</dd></div>
          <div><dt>{{ $t('bayesNode.limits.memory') }}</dt><dd>{{ BAYES_NODE_GROUP_DEFAULTS.memory }}</dd></div>
          <div><dt>{{ $t('bayesNode.limits.cpu') }}</dt><dd>{{ BAYES_NODE_GROUP_DEFAULTS.cpu }}</dd></div>
          <div><dt>{{ $t('bayesNode.limits.tmp') }}</dt><dd>{{ BAYES_NODE_GROUP_DEFAULTS.tmp }}</dd></div>
        </dl>
        <p class="hint">{{ $t('bayesNode.limits.admin') }}</p>
      </div>
    </div>

    <!-- 7. Run -->
    <div class="block">
      <ModelRun
        :outputs="outputs"
        :trial="trial"
        :flow-dirty="flowDirty"
        :disabled="disabled"
        :why="off ? '' : why"
        :read-only="readOnly"
        @run="emit('run')"
      />
    </div>
  </div>
</template>

<style scoped>
.bayes-panel { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.row { display: flex; align-items: center; gap: 8px; }
.row h3 { flex: 1; margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 14px; }
.del { border: 0; background: none; color: var(--muted); cursor: pointer; padding: 4px; border-radius: 6px; }
.del:hover { color: var(--danger); background: var(--bg-soft); }
.block { display: flex; flex-direction: column; gap: 8px; padding: 10px 0 0; border: 0; border-top: 1px solid var(--border-soft); margin: 0; min-width: 0; }
.block.first { border-top: 0; padding-top: 0; }
.block h4, .bhead { margin: 0; padding: 0; font-size: 12px; font-weight: 600; color: var(--text); }
.lbl { font-size: 12px; color: var(--muted); padding: 0; }
.hint { margin: 0; font-size: 11.5px; line-height: 1.45; color: var(--muted); }
.version { overflow-wrap: anywhere; }
.note { margin: 0; display: flex; align-items: flex-start; gap: 6px; font-size: 12px; line-height: 1.4; color: var(--muted); }
.note svg { flex: none; margin-top: 2px; }
.note.warn { color: var(--warning); }
.btnlink { align-self: flex-start; display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--accent); }
.btnlink:hover { text-decoration: underline; }
.btnlink:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.notice { display: flex; flex-direction: column; gap: 8px; padding: 8px 10px; border: 1px solid var(--warning); border-radius: 8px; background: color-mix(in srgb, var(--warning) 9%, transparent); }
.notice p { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12px; line-height: 1.4; color: var(--text); }
.notice p svg { flex: none; margin-top: 2px; color: var(--warning); }
.notice button { align-self: flex-start; }
.moved { display: flex; flex-direction: column; gap: 4px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); }
.moved p { margin: 0; font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
.vars { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
.var { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px; min-width: 0; }
.var > :deep(.sel-trigger), .var > :deep(.select) { flex-basis: 100%; }
.vname { display: inline-flex; align-items: baseline; gap: 6px; font-size: 13px; font-weight: 600; color: var(--text); min-width: 0; }
.vlabel { overflow-wrap: anywhere; }
.vcode { font-size: 11px; color: var(--muted); font-weight: 400; }
.vtype { font-size: 11.5px; color: var(--muted); }
.tag { font-size: 10.5px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
.var .note, .var .hint { flex-basis: 100%; }
.numbers { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 8px; }
.numbers.three { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.nfield { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.nfield label, .nfield > label { font-size: 12px; color: var(--muted); }
.nfield input { min-width: 0; width: 100%; }
.nrow { display: flex; align-items: center; gap: 4px; }
.nrow input { flex: 1; }
.def { font-size: 11px; line-height: 1.35; color: var(--muted); }
.icon { flex: none; border: 0; background: none; color: var(--muted); cursor: pointer; padding: 5px; border-radius: 6px; }
.icon:hover { color: var(--text); background: var(--bg-soft); }
.advanced { border: 1px solid var(--border-soft); border-radius: 8px; padding: 2px 10px; }
.advanced[open] { padding-bottom: 10px; }
.advanced > :not(summary) { margin-top: 10px; }
.advanced > summary { cursor: pointer; font-size: 12px; color: var(--muted); padding: 4px 0; }
.advanced summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.field { border: 0; margin: 0; padding: 0; min-width: 0; }
fieldset.field { display: flex; flex-direction: column; gap: 6px; }
.chk { display: flex; align-items: flex-start; gap: 7px; font-size: 13px; color: var(--text); }
.chk input { width: auto; flex: none; margin-top: 3px; }
.opt { padding: 2px 0; }
.opt > span { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.oname { font-size: 13px; }
.odesc { font-size: 11.5px; line-height: 1.4; color: var(--muted); }
.indent { margin-left: 22px; }
.group-limits { display: flex; flex-direction: column; gap: 6px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); }
.group-limits dl { margin: 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 12px; }
.group-limits dl > div { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; }
.group-limits dt { color: var(--muted); }
.group-limits dd { margin: 0; color: var(--text); font-variant-numeric: tabular-nums; }
</style>
