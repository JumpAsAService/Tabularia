<script setup lang="ts">
// The form of a model node (`type: model`): the keys of its model, read from the catalogue and
// drawn each by its kind's control — a column of the step's input, some columns, a reference level
// for each category, pairs of predictors, a choice, a number, a switch — and only the keys that
// apply to the variant the others make (turning a penalty on hides `se` and shows λ, folds and
// mixing; `orders = auto` hides the orders and shows their maxima). Nothing here names a model: a
// new one in the catalogue gets its form by being added there (composables/modelCatalog.ts).
//
// Below the model's keys: the MLflow block (a connection of the kind `mlflow`, the experiment, the
// name to register the model under), the timeout and the retries, and a slot `run` for the button
// that tries the node on the sample (the trial's lane puts it there).
//
// Every control is a native one or the app's Select: Tab and Shift+Tab move, Space and Enter
// choose, the arrows move among the buttons of a choice. No key of this form needs F, Home, End or
// Menu, on a Mac or on a laptop without them.
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Info, TriangleAlert, Plus, X } from 'lucide-vue-next'
import type { ColumnInfo } from '~/composables/useApi'
import { isSuperseded } from '~/composables/usePreviewSlots'
import { statLevelLabels } from '~/composables/statTests'
import { vDraft } from '~/composables/draftField'
import {
  modelApplies, modelChoiceWordKey, modelColumnFits, modelFieldWordKey, modelFields, modelForbidden, modelGiven, modelGroupKey,
  modelHintKey, modelIsCategorical, modelMlflow, modelMlflowProblem, modelNumber, modelNumberProblem, modelNumbers, modelOf,
  modelOverlaps, modelReasonKey, modelRequiredReason, modelSeason, modelSet, modelTaken, modelUnset,
  MODEL_RETRIES_MAX, MODEL_TIMEOUT_MIN, MODEL_TIMEOUT_SECONDS, type ModelField, type ModelOff,
} from '~/composables/modelCatalog'

const props = defineProps<{
  nodeId: string
  // the node's data: model, params, mlflow, timeoutSeconds, retries
  data: Record<string, any>
  inputColumns: ColumnInfo[]
  // the node's columns are being resolved (preview upstream in progress)
  columnsLoading?: boolean
  // loads a column's distinct values at the node's input (the reference levels, the event value)
  fetchDistinct?: (column: string) => Promise<any[]>
  // the connections of the person: the `mlflow` ones are offered
  connections?: { id: number; name: string; db_type: string; project_id?: number }[]
  // why the model's group cannot run for this person: the form still shows, and says it
  off?: ModelOff
  // the group's longest run, when the administrator set one
  maxTimeout?: number | null
  readOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'update', patch: Record<string, any>): void }>()
const { t, te } = useI18n()

const model = computed(() => modelOf(props.data?.model))
const params = computed<Record<string, any>>(() => props.data?.params ?? {})
// a key that matters only with some values of another is shown only then
const fields = computed<ModelField[]>(() => {
  const m = model.value
  return m ? modelFields(m).filter((f) => modelApplies(m, f.key, params.value)) : []
})
const idOf = (key: string, part = '') => `model-${props.nodeId}-${key}${part ? `-${part}` : ''}`
const has = (key: string) => te(key)
const labelOf = (f: ModelField) => (model.value ? t(modelFieldWordKey(model.value, f.key, 'params', has)) : f.key)
const hintOf = (f: ModelField) => {
  const key = model.value ? modelFieldWordKey(model.value, f.key, 'hints', has) : ''
  return key && te(key) ? t(key) : ''
}
const choiceLabel = (f: ModelField, c: string) => t(modelChoiceWordKey(f.kind, c, has))
const dtypeOf = (column: unknown) => props.inputColumns.find((c) => c.name === column)?.dtype ?? ''

function set(key: string, value: unknown) {
  if (model.value) emit('update', { params: modelSet(model.value, params.value, key, value) })
}

// what the node still lacks, said once, on top: the empty field is not the place to find out
const lacking = computed(() => (model.value ? modelUnset(model.value, params.value) : []))
const overlaps = computed(() => (model.value ? modelOverlaps(model.value, params.value) : []))
const fieldLabel = (key: string) => {
  const f = fields.value.find((x) => x.key === key)
  return f ? labelOf(f) : key
}

// ── why the node cannot run here ─────────────────────────────────────────────
const offText = computed(() => {
  switch (props.off) {
    case 'kubernetes': return t('scripting.kubernetesOnly')
    case 'not_configured': return t('models.node.notConfigured')
    case 'not_enabled': return t('models.node.groupOff', { group: model.value ? t(modelGroupKey(model.value.group)) : '' })
    case 'not_allowed': return t('models.node.groupNotAllowed')
    default: return ''
  }
})

// ── one column ───────────────────────────────────────────────────────────────
// Only the columns the key can use, and not the ones another key of the model already has (the
// outcome is not a predictor); the one already chosen stays listed whatever it is (a type
// changed upstream), with what is wrong, so that the form never shows an empty box over a
// parameter the node still has.
function usableColumns(f: ModelField): ColumnInfo[] {
  const taken = model.value ? modelTaken(model.value, params.value, f.key) : new Set<string>()
  return props.inputColumns.filter((c) => modelColumnFits(f, c.dtype) && !taken.has(c.name))
}
function columnOptions(f: ModelField) {
  const usable = usableColumns(f)
  const options: { value: string | undefined; label: string; hint?: string }[] = usable.map((c) => ({ value: c.name, label: c.name }))
  const current = params.value[f.key]
  if (typeof current === 'string' && current && !usable.some((c) => c.name === current) && props.inputColumns.length) {
    const known = props.inputColumns.some((c) => c.name === current)
    options.unshift({ value: current, label: current, hint: t(known ? 'models.form.notUsable' : 'models.form.notInInput') })
  }
  return f.required ? options : [{ value: undefined, label: '—' }, ...options]
}
const noUsableColumn = (f: ModelField) => props.inputColumns.length > 0 && !!(f.numeric || f.types) && !props.inputColumns.some((c) => modelColumnFits(f, c.dtype))
const noUsableText = (f: ModelField) => t(f.types ? (f.types.includes('number') ? 'models.form.noOrderColumns' : 'models.form.noTimeColumns') : 'models.form.noNumericColumns')

// ── some columns (the predictors, the exogenous series) ──────────────────────
// A list of boxes in the order they were ticked (the order of the terms); past a dozen a filter.
const filters = reactive<Record<string, string>>({})
const FILTER_FROM = 12
function listedColumns(f: ModelField): { name: string; missing: boolean }[] {
  const chosen: string[] = Array.isArray(params.value[f.key]) ? params.value[f.key] : []
  const names = usableColumns(f).map((c) => c.name)
  const q = (filters[f.key] ?? '').trim().toLowerCase()
  const list = names.filter((n) => !q || n.toLowerCase().includes(q)).map((name) => ({ name, missing: false }))
  // a column chosen that the input no longer offers stays, ticked, with its reason
  const gone = chosen.filter((c) => !names.includes(c)).map((name) => ({ name, missing: true }))
  return [...gone, ...list]
}
function toggleColumn(f: ModelField, name: string, on: boolean) {
  const now: string[] = Array.isArray(params.value[f.key]) ? [...params.value[f.key]] : []
  const next = on ? (now.includes(name) ? now : [...now, name]) : now.filter((c) => c !== name)
  set(f.key, next)
}
const columnsFilterShown = (f: ModelField) => usableColumns(f).length > FILTER_FROM

// ── the values of a column (the reference levels, the event) ─────────────────
// Read at the node's input when the column is known, written as the engine labels a group.
interface Levels { state: 'loading' | 'done' | 'failed'; labels: string[] }
const levels = reactive<Record<string, Levels>>({})
const known = new Map<string, unknown[]>()
async function loadLevels(column: string) {
  if (!column || (levels[column] && levels[column]!.state !== 'failed')) return
  const have = known.get(column)
  const labelsOf = (values: unknown[]) => statLevelLabels(values, dtypeOf(column))
  if (have) {
    levels[column] = { state: 'done', labels: labelsOf(have) }
    return
  }
  if (!props.fetchDistinct) {
    levels[column] = { state: 'failed', labels: [] }
    return
  }
  levels[column] = { state: 'loading', labels: [] }
  try {
    const values = await props.fetchDistinct(column)
    known.set(column, values)
    levels[column] = { state: 'done', labels: labelsOf(values) }
  } catch (e) {
    // «overtaken by a newer request» is not a failure: the newer one answers
    if (isSuperseded(e)) delete levels[column]
    else levels[column] = { state: 'failed', labels: [] }
  }
}

// the columns whose values the form shows: a categorical predictor's (its reference), the column a
// level is of (the event's value)
const categoricalPredictors = (f: ModelField): string[] => {
  const of = Array.isArray(params.value[f.of ?? '']) ? (params.value[f.of ?? ''] as string[]) : []
  return of.filter((c) => props.inputColumns.some((x) => x.name === c && modelIsCategorical(x.dtype)))
}
const levelColumn = (f: ModelField): string => (typeof params.value[f.of ?? ''] === 'string' ? params.value[f.of ?? ''] : '')
watch(
  () => [props.nodeId, props.inputColumns.length, ...fields.value.flatMap((f) =>
    f.control === 'reference' ? categoricalPredictors(f) : f.control === 'level' ? [levelColumn(f)] : [])].join('\u0001'),
  (now, before) => {
    if (before && now.split('\u0001')[0] !== before.split('\u0001')[0]) {
      known.clear()
      for (const k of Object.keys(levels)) delete levels[k]
    }
    for (const f of fields.value) {
      if (f.control === 'reference') categoricalPredictors(f).forEach(loadLevels)
      else if (f.control === 'level') loadLevels(levelColumn(f))
    }
  },
  { immediate: true },
)
const levelLabels = (column: string) => levels[column]?.labels ?? []

const shownValue = (f: ModelField) => (params.value[f.key] === undefined || params.value[f.key] === null ? '' : String(params.value[f.key]))

function referenceOptions(f: ModelField, column: string) {
  const labels = levelLabels(column)
  const current = (params.value[f.key] ?? {})[column]
  const options: { value: string; label: string; hint?: string }[] = labels.map((l) => ({ value: l, label: l }))
  if (current !== undefined && !labels.includes(String(current)) && levels[column]?.state === 'done') {
    options.unshift({ value: String(current), label: String(current), hint: t('models.form.notInData') })
  }
  return [{ value: '', label: t('models.form.firstLevel', { level: labels[0] ?? '…' }) }, ...options]
}
function setReference(f: ModelField, column: string, level: unknown) {
  const next: Record<string, unknown> = { ...(params.value[f.key] ?? {}) }
  if (typeof level === 'string' && level) next[column] = level
  else delete next[column]
  set(f.key, next)
}

// ── pairs of predictors (the interactions) ───────────────────────────────────
// A pair is saved when both its places are filled; a half-made one stays here.
const pairDraft = ref<[string, string][]>([])
watch(() => props.nodeId, () => { pairDraft.value = [] })
const savedPairs = (f: ModelField): [string, string][] => (Array.isArray(params.value[f.key]) ? params.value[f.key] : [])
const pairChoices = (f: ModelField): string[] => (Array.isArray(params.value[f.of ?? '']) ? params.value[f.of ?? ''] : [])
const pairOptions = (f: ModelField) => pairChoices(f).map((c) => ({ value: c, label: c }))
function setPair(f: ModelField, index: number, place: 0 | 1, value: unknown) {
  const saved = savedPairs(f)
  const row: [string, string] = index < saved.length ? [...saved[index]!] as [string, string] : [...pairDraft.value[index - saved.length]!] as [string, string]
  row[place] = typeof value === 'string' ? value : ''
  const list = saved.map((p) => [...p] as [string, string])
  const draft = pairDraft.value.map((p) => [...p] as [string, string])
  if (index < saved.length) list[index] = row
  else draft[index - saved.length] = row
  const [ready, rest] = [list, draft].flat().reduce<[[string, string][], [string, string][]]>((acc, p) => {
    ;(p[0] && p[1] && p[0] !== p[1] ? acc[0] : acc[1]).push(p)
    return acc
  }, [[], []])
  pairDraft.value = rest
  set(f.key, ready)
}
function addPair() { pairDraft.value = [...pairDraft.value, ['', '']] }
function removePair(f: ModelField, index: number) {
  const saved = savedPairs(f)
  if (index < saved.length) set(f.key, saved.filter((_, i) => i !== index))
  else pairDraft.value = pairDraft.value.filter((_, i) => i !== index - saved.length)
}
const pairRows = (f: ModelField): [string, string][] => [...savedPairs(f), ...pairDraft.value]
const pairsFull = (f: ModelField) => pairRows(f).length >= (f.max ?? Infinity)

// ── a number ─────────────────────────────────────────────────────────────────
function numberProblem(f: ModelField, value: unknown = params.value[f.key]): string {
  const problem = modelNumberProblem(f, value)
  return problem ? t(`models.form.${problem}`, { above: f.above ?? '', below: f.below ?? '', min: f.min ?? '', max: f.max ?? '' }) : ''
}
const defaultOf = (f: ModelField) => {
  const d = model.value?.defaults[f.key]
  if (f.key === 's') return String(modelSeason(params.value) ?? '')
  return d === null || d === undefined || Array.isArray(d) ? '' : String(d)
}
// a number written in the box: the node keeps it as a number when it is one, and the box says what is
// wrong with it (the model refuses it, as it would refuse a number outside its bounds, at the run)
function setNumber(f: ModelField, raw: string) {
  set(f.key, raw.trim() === '' ? undefined : raw)
}

// ── a number or one of a few words (λ: cv_1se, cv_min, a value) ──────────────
const chosenNumberOrChoice = (f: ModelField): string => {
  const v = params.value[f.key] ?? model.value?.defaults[f.key]
  return typeof v === 'string' ? v : '__number__'
}
function pickNumberOrChoice(f: ModelField, c: string) {
  set(f.key, c === '__number__' ? numberOr(f) : c)
}
// the number a box starts from when «a value» is chosen: the middle of mixing's range, else 1
const numberOr = (f: ModelField): number => {
  const v = params.value[f.key]
  return typeof v === 'number' ? v : f.key === 'mixing' ? 0.5 : 1
}

// ── a list of numbers (the times to report survival at) ──────────────────────
const timesText = (f: ModelField) => (Array.isArray(params.value[f.key]) ? params.value[f.key].join(', ') : '')
const timesProblem = (f: ModelField): string => {
  const list = modelNumbers(params.value[f.key])
  const bad = list.find((n) => (f.min !== undefined && n < f.min))
  if (bad !== undefined) return t('models.form.atLeast', { min: f.min })
  if (f.max_count !== undefined && list.length > f.max_count) return t('models.form.tooMany', { n: f.max_count })
  return ''
}

// ── a choice ─────────────────────────────────────────────────────────────────
const chosen = (f: ModelField) => params.value[f.key] ?? model.value?.defaults[f.key]
function forbidden(f: ModelField, value: unknown): string {
  const rule = model.value ? modelForbidden(model.value, f.key, value, params.value) : null
  if (!rule) return ''
  const key = modelReasonKey(rule.reason)
  return te(key) ? t(key) : rule.message
}
// a few choices are buttons (the arrows move among them); many are a list to search
const FEW = 4
const asButtons = (f: ModelField) => f.choices.length <= FEW
const choiceOptions = (f: ModelField) => f.choices.map((c) => ({ value: c, label: choiceLabel(f, c), hint: forbidden(f, c) || undefined }))

// what a key asks for when it is not given yet (WLS needs the weights): the sentence, with the
// words of the language when there are some
const requiredNote = (f: ModelField): string => {
  if (!model.value || modelGiven(params.value[f.key])) return ''
  const rule = modelRequiredReason(model.value, f.key, params.value)
  if (!rule) return ''
  const key = modelReasonKey(rule.reason)
  return te(key) ? t(key) : rule.message
}

// ── MLflow ───────────────────────────────────────────────────────────────────
const mlflowConnections = computed(() => (props.connections ?? []).filter((c) => c.db_type === 'mlflow'))
const mlflow = computed(() => props.data?.mlflow ?? null)
const mlflowOptions = computed(() => [
  { value: null, label: t('models.mlflow.none') },
  ...mlflowConnections.value.map((c) => ({ value: c.id, label: c.name })),
])
// a connection the person cannot use any more stays listed: the node still holds it
const mlflowOptionsShown = computed(() => {
  const id = mlflow.value?.connectionId
  return typeof id === 'number' && !mlflowConnections.value.some((c) => c.id === id)
    ? [...mlflowOptions.value, { value: id, label: t('models.mlflow.unknownConnection', { id }) }]
    : mlflowOptions.value
})
function pickConnection(id: unknown) {
  if (typeof id !== 'number') {
    emit('update', { mlflow: undefined })
    return
  }
  emit('update', { mlflow: modelMlflow({ ...(mlflow.value ?? {}), connectionId: id }) })
}
function setMlflow(key: 'experiment' | 'registerAs' | 'continueWithout', value: unknown) {
  emit('update', { mlflow: modelMlflow({ ...(mlflow.value ?? {}), [key]: value }) })
}
const mlflowProblem = computed(() => modelMlflowProblem(mlflow.value))

// ── the run's limits ─────────────────────────────────────────────────────────
const timeoutMax = computed(() => props.maxTimeout ?? 86400)
function limit(key: 'timeoutSeconds' | 'retries', raw: string, min: number, max: number) {
  const n = modelNumber(raw)
  if (!Number.isFinite(n)) return
  emit('update', { [key]: Math.min(max, Math.max(min, Math.round(n))) })
}
</script>

<template>
  <fieldset v-if="model" class="modelform" :disabled="readOnly">
    <p class="about">{{ $t(modelHintKey(model.id)) }}</p>

    <p v-if="offText" class="note warn" role="status"><TriangleAlert :size="13" aria-hidden="true" /> {{ offText }}</p>

    <!-- columns being resolved: a skeleton in place of the false "no column" -->
    <div v-if="columnsLoading && !inputColumns.length" class="skcols" aria-busy="true">
      <span class="sk" style="width: 68%" />
      <span class="sk" style="width: 52%" />
      <span class="sk" style="width: 61%" />
    </div>
    <div v-else-if="!inputColumns.length" class="note">
      <Info :size="13" aria-hidden="true" /> {{ $t('paramForm.noUpstreamColumns') }}
    </div>

    <p v-if="lacking.length" class="note" role="status">
      <Info :size="13" aria-hidden="true" /> {{ $t('models.form.toSet', { keys: lacking.map(fieldLabel).join(', ') }) }}
    </p>
    <p v-for="o in overlaps" :key="o.column" class="note warn" role="alert">
      <TriangleAlert :size="13" aria-hidden="true" /> {{ $t('models.form.overlap', { column: o.column, keys: o.keys.map(fieldLabel).join(' / ') }) }}
    </p>

    <template v-for="f in fields" :key="f.key">
      <!-- one column of the step's input -->
      <div v-if="f.control === 'column'" class="field">
        <span :id="idOf(f.key, 'label')" class="lbl">{{ labelOf(f) }}<span v-if="f.required" class="req" aria-hidden="true"> *</span></span>
        <Select
          :id="idOf(f.key)"
          :model-value="params[f.key]"
          :options="columnOptions(f)"
          searchable
          placeholder="—"
          :aria-labelledby="idOf(f.key, 'label')"
          :aria-describedby="hintOf(f) ? idOf(f.key, 'hint') : undefined"
          @update:model-value="(v: any) => set(f.key, v)"
        />
        <p v-if="noUsableColumn(f)" class="note warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ noUsableText(f) }}</p>
        <p v-else-if="requiredNote(f)" class="note warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ requiredNote(f) }}</p>
        <p v-else-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- some columns: boxes, in the order they were ticked -->
      <fieldset v-else-if="f.control === 'columns'" class="field choices">
        <legend class="lbl">{{ labelOf(f) }}<span v-if="f.required" class="req" aria-hidden="true"> *</span></legend>
        <input
          v-if="columnsFilterShown(f)"
          v-model="filters[f.key]"
          type="search"
          autocomplete="off"
          :placeholder="$t('models.form.filterColumns')"
          :aria-label="`${labelOf(f)}: ${$t('models.form.filterColumns')}`"
        />
        <div class="colist" role="group" :aria-label="labelOf(f)">
          <label v-for="c in listedColumns(f)" :key="c.name" class="chk">
            <input
              type="checkbox"
              :checked="(params[f.key] ?? []).includes(c.name)"
              :aria-describedby="hintOf(f) ? idOf(f.key, 'hint') : undefined"
              @change="toggleColumn(f, c.name, ($event.target as HTMLInputElement).checked)"
            />
            <span>
              {{ c.name }}
              <span v-if="c.missing" class="why warn">{{ $t('models.form.notInInput') }}</span>
              <span v-else-if="f.predictor && !modelColumnFits(f, dtypeOf(c.name))" class="why">{{ $t('models.form.notUsable') }}</span>
            </span>
          </label>
          <p v-if="!listedColumns(f).length" class="muted small">{{ $t('models.form.noColumnsLeft') }}</p>
        </div>
        <p v-if="noUsableColumn(f)" class="note warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ noUsableText(f) }}</p>
        <p v-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </fieldset>

      <!-- one value of a column: chosen among its values, or written -->
      <div v-else-if="f.control === 'level'" class="field">
        <label :for="idOf(f.key)" class="lbl">{{ labelOf(f) }}<span v-if="f.required" class="req" aria-hidden="true"> *</span></label>
        <input
          :id="idOf(f.key)"
          type="text"
          autocomplete="off"
          :list="idOf(f.key, 'values')"
          :value="shownValue(f)"
          v-draft="shownValue(f)"
          :placeholder="levelColumn(f) ? $t('models.form.levelPlaceholder') : $t('models.form.chooseColumnFirst', { field: fieldLabel(f.of ?? '') })"
          :disabled="!levelColumn(f)"
          :aria-describedby="hintOf(f) ? idOf(f.key, 'hint') : undefined"
          @change="set(f.key, ($event.target as HTMLInputElement).value)"
        />
        <datalist :id="idOf(f.key, 'values')" :aria-label="$t('models.form.valuesOf', { column: levelColumn(f) })">
          <option v-for="l in levelLabels(levelColumn(f))" :key="l" :value="l" />
        </datalist>
        <p v-if="levels[levelColumn(f)]?.state === 'failed'" class="note warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('models.form.levelsFailed') }}</p>
        <p v-else-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- the reference level of each categorical predictor -->
      <fieldset v-else-if="f.control === 'reference'" v-show="categoricalPredictors(f).length" class="field choices">
        <legend class="lbl">{{ labelOf(f) }}</legend>
        <div v-for="c in categoricalPredictors(f)" :key="c" class="place">
          <span class="sharel" :title="c">{{ c }}</span>
          <span v-if="levels[c]?.state === 'loading'" class="sk" style="width: 110px" aria-busy="true" />
          <Select
            v-else
            :model-value="(params[f.key] ?? {})[c] !== undefined ? String((params[f.key] ?? {})[c]) : ''"
            :options="referenceOptions(f, c)"
            searchable
            :aria-label="`${labelOf(f)}: ${c}`"
            @update:model-value="(v: any) => setReference(f, c, v)"
          />
        </div>
        <p v-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
      </fieldset>

      <!-- pairs of predictors -->
      <fieldset v-else-if="f.control === 'pairs'" class="field choices">
        <legend class="lbl">{{ labelOf(f) }}</legend>
        <div v-for="(row, i) in pairRows(f)" :key="i" class="place pair">
          <Select
            :model-value="row[0]"
            :options="pairOptions(f)"
            :aria-label="`${labelOf(f)} ${i + 1}: ${$t('models.form.first')}`"
            @update:model-value="(v: any) => setPair(f, i, 0, v)"
          />
          <span class="times" aria-hidden="true">×</span>
          <Select
            :model-value="row[1]"
            :options="pairOptions(f)"
            :aria-label="`${labelOf(f)} ${i + 1}: ${$t('models.form.second')}`"
            @update:model-value="(v: any) => setPair(f, i, 1, v)"
          />
          <button type="button" class="icon" :title="$t('models.form.removePair')" :aria-label="`${$t('models.form.removePair')} ${i + 1}`" @click="removePair(f, i)">
            <X :size="13" aria-hidden="true" />
          </button>
        </div>
        <button type="button" class="add" :disabled="pairChoices(f).length < 2 || pairsFull(f)" @click="addPair">
          <Plus :size="13" aria-hidden="true" /> {{ $t('models.form.addPair') }}
        </button>
        <p v-if="pairChoices(f).length < 2" class="hint">{{ $t('models.form.pairsNeedPredictors') }}</p>
        <p v-else-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
      </fieldset>

      <!-- on or off -->
      <div v-else-if="f.control === 'boolean'" class="field">
        <label class="chk">
          <input
            type="checkbox"
            role="switch"
            :checked="chosen(f) === true"
            :aria-describedby="hintOf(f) ? idOf(f.key, 'hint') : undefined"
            @change="set(f.key, ($event.target as HTMLInputElement).checked)"
          />
          {{ labelOf(f) }}
        </label>
        <p v-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- a number, written when the box is left, as every field of the panel -->
      <div v-else-if="f.control === 'number' || f.control === 'integer'" class="field">
        <label :for="idOf(f.key)" class="lbl">{{ labelOf(f) }}</label>
        <input
          :id="idOf(f.key)"
          type="number"
          step="any"
          :value="params[f.key] ?? ''"
          v-draft="params[f.key] ?? ''"
          :placeholder="defaultOf(f)"
          :aria-invalid="numberProblem(f) ? 'true' : undefined"
          :aria-describedby="numberProblem(f) ? idOf(f.key, 'problem') : hintOf(f) ? idOf(f.key, 'hint') : undefined"
          @change="setNumber(f, ($event.target as HTMLInputElement).value)"
        />
        <p v-if="numberProblem(f)" :id="idOf(f.key, 'problem')" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ numberProblem(f) }}</p>
        <p v-else-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- a list of numbers -->
      <div v-else-if="f.control === 'numbers'" class="field">
        <label :for="idOf(f.key)" class="lbl">{{ labelOf(f) }}</label>
        <input
          :id="idOf(f.key)"
          type="text"
          inputmode="decimal"
          autocomplete="off"
          :value="timesText(f)"
          v-draft="timesText(f)"
          :placeholder="$t('models.form.numbersPlaceholder')"
          :aria-invalid="timesProblem(f) ? 'true' : undefined"
          :aria-describedby="timesProblem(f) ? idOf(f.key, 'problem') : hintOf(f) ? idOf(f.key, 'hint') : undefined"
          @change="set(f.key, ($event.target as HTMLInputElement).value)"
        />
        <p v-if="timesProblem(f)" :id="idOf(f.key, 'problem')" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ timesProblem(f) }}</p>
        <p v-else-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- a few words or a number -->
      <fieldset v-else-if="f.control === 'number_or_choice'" class="field choices">
        <legend class="lbl">{{ labelOf(f) }}</legend>
        <label v-for="c in f.choices" :key="c" class="chk">
          <input type="radio" :name="idOf(f.key)" :checked="chosenNumberOrChoice(f) === c" @change="pickNumberOrChoice(f, c)" />
          <span>{{ choiceLabel(f, c) }}</span>
        </label>
        <label class="chk">
          <input type="radio" :name="idOf(f.key)" :checked="chosenNumberOrChoice(f) === '__number__'" @change="pickNumberOrChoice(f, '__number__')" />
          <span>{{ $t('models.form.aValue') }}</span>
        </label>
        <input
          v-if="chosenNumberOrChoice(f) === '__number__'"
          type="number"
          step="any"
          :value="params[f.key] ?? ''"
          v-draft="params[f.key] ?? ''"
          :aria-label="`${labelOf(f)}: ${$t('models.form.aValue')}`"
          :aria-invalid="numberProblem(f) ? 'true' : undefined"
          @change="setNumber(f, ($event.target as HTMLInputElement).value)"
        />
        <p v-if="numberProblem(f)" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ numberProblem(f) }}</p>
        <p v-else-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
      </fieldset>

      <!-- one of a few choices: native radio buttons, the arrows move among them; of many: a list -->
      <fieldset v-else-if="f.control === 'choice' && asButtons(f)" class="field choices">
        <legend class="lbl">{{ labelOf(f) }}</legend>
        <label v-for="c in f.choices" :key="c" class="chk" :class="{ off: !!forbidden(f, c) && chosen(f) !== c }">
          <input
            type="radio"
            :name="idOf(f.key)"
            :value="c"
            :checked="chosen(f) === c"
            :disabled="!!forbidden(f, c) && chosen(f) !== c"
            :aria-invalid="forbidden(f, c) && chosen(f) === c ? 'true' : undefined"
            @change="set(f.key, c)"
          />
          <span>
            {{ choiceLabel(f, c) }}
            <span v-if="forbidden(f, c)" class="why" :class="{ warn: chosen(f) === c }">{{ forbidden(f, c) }}</span>
          </span>
        </label>
        <p v-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
      </fieldset>
      <div v-else-if="f.control === 'choice'" class="field">
        <span :id="idOf(f.key, 'label')" class="lbl">{{ labelOf(f) }}<span v-if="f.required" class="req" aria-hidden="true"> *</span></span>
        <Select
          :id="idOf(f.key)"
          :model-value="chosen(f)"
          :options="choiceOptions(f)"
          :placeholder="'—'"
          :aria-labelledby="idOf(f.key, 'label')"
          :aria-describedby="hintOf(f) ? idOf(f.key, 'hint') : undefined"
          @update:model-value="(v: any) => set(f.key, v)"
        />
        <p v-if="forbidden(f, chosen(f))" class="note warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ forbidden(f, chosen(f)) }}</p>
        <p v-else-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>
    </template>

    <!-- MLflow: where the run of the flow is logged, and the model registered -->
    <section class="block" :aria-labelledby="idOf('mlflow', 'title')">
      <h4 :id="idOf('mlflow', 'title')">{{ $t('models.mlflow.title') }}</h4>
      <div class="field">
        <span :id="idOf('mlflow', 'connection-label')" class="lbl">{{ $t('models.mlflow.connection') }}</span>
        <Select
          :model-value="mlflow?.connectionId ?? null"
          :options="mlflowOptionsShown"
          :aria-labelledby="idOf('mlflow', 'connection-label')"
          @update:model-value="pickConnection"
        />
        <p v-if="!mlflowConnections.length" class="hint">{{ $t('models.mlflow.noConnection') }}</p>
        <p v-else-if="!mlflow" class="hint">{{ $t('models.mlflow.hint') }}</p>
      </div>
      <template v-if="mlflow">
        <div class="field">
          <label :for="idOf('mlflow', 'experiment')" class="lbl">{{ $t('models.mlflow.experiment') }}<span class="req" aria-hidden="true"> *</span></label>
          <input
            :id="idOf('mlflow', 'experiment')"
            type="text"
            autocomplete="off"
            :value="mlflow.experiment ?? ''"
            v-draft="mlflow.experiment ?? ''"
            :placeholder="$t('models.mlflow.experimentPlaceholder')"
            :aria-invalid="mlflowProblem ? 'true' : undefined"
            :aria-describedby="mlflowProblem ? idOf('mlflow', 'problem') : undefined"
            @change="setMlflow('experiment', ($event.target as HTMLInputElement).value)"
          />
          <p v-if="mlflowProblem" :id="idOf('mlflow', 'problem')" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('models.mlflow.experimentNeeded') }}</p>
        </div>
        <div class="field">
          <label :for="idOf('mlflow', 'register')" class="lbl">{{ $t('models.mlflow.registerAs') }}</label>
          <input
            :id="idOf('mlflow', 'register')"
            type="text"
            autocomplete="off"
            :value="mlflow.registerAs ?? ''"
            v-draft="mlflow.registerAs ?? ''"
            :placeholder="$t('models.mlflow.registerPlaceholder')"
            :aria-describedby="idOf('mlflow', 'register-hint')"
            @change="setMlflow('registerAs', ($event.target as HTMLInputElement).value)"
          />
          <p :id="idOf('mlflow', 'register-hint')" class="hint">{{ $t('models.mlflow.registerHint') }}</p>
        </div>
        <div class="field">
          <label class="chk">
            <input type="checkbox" role="switch" :checked="mlflow.continueWithout === true" @change="setMlflow('continueWithout', ($event.target as HTMLInputElement).checked)" />
            {{ $t('models.mlflow.continueWithout') }}
          </label>
          <p class="hint">{{ $t('models.mlflow.continueHint') }}</p>
        </div>
      </template>
    </section>

    <!-- the run's limits -->
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
      <p v-if="maxTimeout" class="hint">{{ $t('models.run.timeoutMax', { n: maxTimeout }) }}</p>
    </section>

    <!-- the trial's button (Run on the sample) and what it left: the trial's lane -->
    <slot name="run" />
  </fieldset>
</template>

<style scoped>
.modelform { display: flex; flex-direction: column; gap: 12px; border: none; margin: 0; padding: 0; min-width: 0; }
.about { margin: 0; font-size: 12px; line-height: 1.45; color: var(--muted); }
.skcols { display: flex; flex-direction: column; gap: 8px; padding: 2px 0; }
.field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
fieldset.field { border: none; margin: 0; padding: 0; }
.lbl, legend.lbl { font-size: 12px; color: var(--muted); padding: 0; }
legend.lbl { margin-bottom: 4px; }
.req { color: var(--muted); }
.hint { margin: 0; font-size: 11.5px; line-height: 1.4; color: var(--muted); }
.muted.small { margin: 0; font-size: 11.5px; }
.note { margin: 0; font-size: 12px; line-height: 1.4; color: var(--muted); display: flex; align-items: flex-start; gap: 5px; }
.note svg { flex-shrink: 0; margin-top: 2px; }
.note.warn, .why.warn { color: var(--warning); }
.chk { display: flex; align-items: flex-start; gap: 6px; color: var(--text); font-size: 13px; }
.chk input { width: auto; flex-shrink: 0; margin-top: 2px; }
.chk.off { color: var(--muted); }
.why { display: block; font-size: 11.5px; color: var(--muted); }
.colist { display: flex; flex-direction: column; gap: 4px; max-height: 190px; overflow-y: auto; padding: 4px 2px; }
.place { display: flex; align-items: center; gap: 6px; }
.place > :deep(.sel-trigger) { flex: 1; min-width: 0; }
.pair .times { color: var(--muted); font-size: 12px; }
.sharel { flex: 1; min-width: 0; font-size: 12.5px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.icon { flex-shrink: 0; border: 0; background: none; color: var(--muted); cursor: pointer; padding: 4px; border-radius: 6px; }
.icon:hover { color: var(--danger); background: var(--bg-soft); }
.add { align-self: flex-start; display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; font-size: 12px; }
.block { display: flex; flex-direction: column; gap: 10px; padding-top: 10px; border-top: 1px solid var(--border-soft); }
.block h4 { margin: 0; font-size: 12px; font-weight: 600; color: var(--text); }
.numbers { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.numbers input { min-width: 0; }
</style>
