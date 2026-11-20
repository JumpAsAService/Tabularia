<script setup lang="ts">
// The form of a statistics node (a `stat_*` operation): the keys of its test, read from the
// catalogue and drawn each by its kind's control — a column of the step's input (numeric
// only where the test needs a number), some of a column's values (the two groups to
// compare), a switch, a number, a choice. Nothing here names a test: a new one in the
// catalogue gets its form by being added there (composables/statTests.ts).
//
// The node works on the rows that reach it, as every step: the form says nothing about
// samples. A choice another option forbids is disabled, with the reason.
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Info, TriangleAlert } from 'lucide-vue-next'
import type { ColumnInfo } from '~/composables/useApi'
import { isSuperseded } from '~/composables/usePreviewSlots'
import { isNumericDtype } from '~/composables/previewGrid'
import {
  STAT_LEVELS_SHOWN, STAT_VALUE_CONTROLS, statApplies, statFieldWordKey, statFields, statForbidden, statHintKey, statLevelLabels,
  statNumberProblem, statReasonKey, statSetChecked, statShares, statSharesProblem, statTestOf, type StatField,
} from '~/composables/statTests'

const props = defineProps<{
  nodeId: string
  opType: string
  params: Record<string, any>
  inputColumns: ColumnInfo[]
  // the node's columns are being resolved (preview upstream in progress)
  columnsLoading?: boolean
  // loads a column's distinct values at the node's input (the groups to choose among)
  fetchDistinct?: (column: string) => Promise<any[]>
}>()
const emit = defineEmits<{ (e: 'update', params: Record<string, any>): void }>()
const { t, te } = useI18n()

const test = computed(() => statTestOf(props.opType))
// a key that matters only with some values of another (the correction of pairwise t tests)
// is shown only then
const fields = computed<StatField[]>(() => {
  const now = test.value
  return now ? statFields(now).filter((f) => statApplies(now, f.key, props.params)) : []
})
const idOf = (key: string, part = '') => `stat-${props.nodeId}-${key}${part ? `-${part}` : ''}`
const has = (key: string) => te(key)
// the test's own words for a key when it has some, else the shared ones
const labelOf = (f: StatField) => (test.value ? t(statFieldWordKey(test.value, f.key, 'params', has)) : f.key)
const hintOf = (f: StatField) => {
  const key = test.value ? statFieldWordKey(test.value, f.key, 'hints', has) : ''
  return key && te(key) ? t(key) : ''
}
const dtypeOf = (column: unknown) => props.inputColumns.find((c) => c.name === column)?.dtype ?? ''

// what the last change put back, in a sentence: a choice the change forbade (Tukey's HSD once
// the variances are not equal) is not sent to the engine, which would refuse it
const repairedNote = ref('')
watch(() => props.nodeId, () => { repairedNote.value = '' })
function set(key: string, value: unknown) {
  if (!test.value) return
  const { params, repaired } = statSetChecked(test.value, props.params, key, value)
  repairedNote.value = repaired.map((r) => {
    const f = fields.value.find((x) => x.key === r.key)
    const reasonKey = statReasonKey(r.rule.reason)
    return t('stat.form.repaired', {
      field: f ? labelOf(f) : r.key,
      choice: f ? t(`stat.choices.${f.kind}.${String(r.to)}`) : String(r.to),
      reason: te(reasonKey) ? t(reasonKey) : r.rule.message,
    })
  }).join(' ')
  emit('update', params)
}

// ── a column ─────────────────────────────────────────────────────────────────
// Only the columns the test can use; the one already chosen stays listed whatever it is
// (a type changed upstream), with what is wrong, so that the form never shows an empty box
// over a parameter the node still has.
function columnOptions(f: StatField) {
  const usable = props.inputColumns.filter((c) => !f.numeric || isNumericDtype(c.dtype))
  const options: { value: string | undefined; label: string; hint?: string }[] = usable.map((c) => ({ value: c.name, label: c.name }))
  const current = props.params[f.key]
  if (typeof current === 'string' && current && !usable.some((c) => c.name === current) && props.inputColumns.length) {
    const known = props.inputColumns.some((c) => c.name === current)
    options.unshift({ value: current, label: current, hint: t(known ? 'stat.form.notNumeric' : 'stat.form.notInInput') })
  }
  return f.required ? options : [{ value: undefined, label: '—' }, ...options]
}
const noUsableColumn = (f: StatField) =>
  !!f.numeric && props.inputColumns.length > 0 && !props.inputColumns.some((c) => isNumericDtype(c.dtype))

// ── some of a column's values (the groups to compare) ────────────────────────
// The values are read at the node's input when the column is chosen, and written as the
// engine labels a group. The choice reaches the node when every place is filled; until
// then it stays here, and the node compares the column's groups by itself.
interface Levels { column: string; state: 'loading' | 'done' | 'failed'; values: unknown[] }
const levels = reactive<Record<string, Levels>>({})
const draft = reactive<Record<string, string[]>>({})
const sharesAsked = reactive<Record<string, boolean>>({}) // proportions chosen as «given», none written yet
const known = new Map<string, unknown[]>() // per column, for this node
// the labels are written from the values and the column's type: the type may arrive later
const labelsOf = (f: StatField): string[] => statLevelLabels(levels[f.key]?.values, dtypeOf(levels[f.key]?.column))

// every field whose values are some of a column's: the groups to compare, the value that is a
// success, the categories that are given a proportion
const levelFields = computed(() => fields.value.filter((f) => STAT_VALUE_CONTROLS.includes(f.control)))
const columnFor = (f: StatField): string => (typeof props.params[f.of ?? ''] === 'string' ? props.params[f.of ?? ''] : '')
const blank = (f: StatField) => Array.from({ length: f.count ?? 2 }, () => '')

async function loadLevels(f: StatField) {
  const column = columnFor(f)
  if (!column) {
    delete levels[f.key]
    return
  }
  if (levels[f.key]?.column === column && levels[f.key]?.state !== 'failed') return
  const have = known.get(column)
  if (have) {
    levels[f.key] = { column, state: 'done', values: have }
    return
  }
  if (!props.fetchDistinct) {
    levels[f.key] = { column, state: 'failed', values: [] }
    return
  }
  levels[f.key] = { column, state: 'loading', values: [] }
  try {
    const values = await props.fetchDistinct(column)
    known.set(column, values)
    if (columnFor(f) === column) levels[f.key] = { column, state: 'done', values }
  } catch (e) {
    // «overtaken by a newer request» is not a failure: the newer one answers
    if (!isSuperseded(e) && columnFor(f) === column) levels[f.key] = { column, state: 'failed', values: [] }
  }
}

// The places follow the node's parameters (Undo and Redo change them from outside); what is
// half chosen is kept while the column stays the same. Another node or another test:
// nothing of the one before stays.
let shownFor = ''
watch(
  () => [props.nodeId, props.opType, ...levelFields.value.flatMap((f) => [f.key, columnFor(f), JSON.stringify(props.params[f.key] ?? null)])].join('\u0001'),
  () => {
    const now = `${props.nodeId}\u0001${props.opType}`
    if (now !== shownFor) {
      shownFor = now
      known.clear()
      for (const k of Object.keys(levels)) delete levels[k]
      for (const k of Object.keys(draft)) delete draft[k]
      for (const k of Object.keys(sharesAsked)) delete sharesAsked[k]
    }
    for (const f of levelFields.value) {
      if (f.control !== 'levels') {
        loadLevels(f)
        continue
      }
      const saved = props.params[f.key]
      const half = draft[f.key]?.some(Boolean) && !draft[f.key]!.every(Boolean)
      if (Array.isArray(saved)) draft[f.key] = saved.map(String)
      else if (!half || levels[f.key]?.column !== columnFor(f)) draft[f.key] = blank(f)
      loadLevels(f)
    }
  },
  { immediate: true },
)

function levelOptions(f: StatField, place: number) {
  const chosen = draft[f.key] ?? blank(f)
  const taken = new Set(chosen.filter((v, i) => v && i !== place))
  const labels = labelsOf(f)
  const options: { value: string; label: string; hint?: string }[] = labels.filter((l) => !taken.has(l)).map((l) => ({ value: l, label: l }))
  const current = chosen[place]
  if (current && !labels.includes(current) && levels[f.key]?.state === 'done') {
    options.unshift({ value: current, label: current, hint: t('stat.form.notInData') })
  }
  return [{ value: '', label: t('stat.form.automatic') }, ...options]
}
function setLevel(f: StatField, place: number, value: unknown) {
  const next = [...(draft[f.key] ?? blank(f))]
  next[place] = typeof value === 'string' ? value : ''
  draft[f.key] = next
  if (next.every(Boolean)) set(f.key, next)
  else if (props.params[f.key] !== undefined) set(f.key, undefined)
}
/** What to say under the places: nothing while all is in order. */
function levelNote(f: StatField): string {
  const column = columnFor(f)
  const got = levels[f.key]
  if (!column) return t('stat.form.chooseColumnFirst', { field: test.value ? t(statFieldWordKey(test.value, f.of ?? '', 'params', has)) : '' })
  if (!got || got.state === 'loading') return ''
  if (got.state === 'failed') return t('stat.form.levelsFailed')
  const count = f.count ?? 2
  const n = labelsOf(f).length
  if (n < count) return t('stat.form.fewLevels', { column, n, count })
  if (n > count && !(draft[f.key] ?? []).every(Boolean)) {
    return t('stat.form.manyLevels', { column, n: n >= STAT_LEVELS_SHOWN ? `${STAT_LEVELS_SHOWN}+` : n, count })
  }
  return ''
}

// ── one value of a column (the success) ──────────────────────────────────────
// A text box with the column's values as suggestions (a native list: arrows and typing, on every
// keyboard): one of them, or another written, as the engine compares it.
const shownValue = (f: StatField) => (props.params[f.key] === undefined || props.params[f.key] === null ? '' : String(props.params[f.key]))

// ── a proportion for each category (a goodness of fit) ──────────────────────
// All equal (nothing given), or a number for each value of the column. «Given» is remembered here
// while nothing is written yet (`sharesAsked`): the node keeps only proportions.
const sharesOf = (f: StatField) => statShares(props.params[f.key])
const sharesGiven = (f: StatField) => !!sharesAsked[f.key] || Object.keys(sharesOf(f)).length > 0
function shareLabels(f: StatField): string[] {
  const labels = labelsOf(f)
  return [...labels, ...Object.keys(sharesOf(f)).filter((l) => !labels.includes(l))]
}
function chooseShares(f: StatField, given: boolean) {
  sharesAsked[f.key] = given
  if (!given && props.params[f.key] !== undefined) set(f.key, undefined)
}
function setShare(f: StatField, label: string, raw: string) {
  const next: Record<string, unknown> = { ...sharesOf(f) }
  if (raw.trim() === '') delete next[label]
  else next[label] = raw
  set(f.key, Object.keys(next).length ? next : undefined)
}
function sharesNote(f: StatField): { text: string; warn: boolean } {
  const shares = sharesOf(f)
  const labels = shareLabels(f)
  const sum = labels.reduce((s, l) => s + (shares[l] ?? 0), 0)
  const shown = String(Number(sum.toFixed(9)))
  const problem = statSharesProblem(shares, labels)
  if (problem === 'missing') return { text: t('stat.form.sharesMissing'), warn: true }
  if (problem === 'sum') return { text: t('stat.form.sharesNotOne', { sum: shown }), warn: true }
  return { text: t('stat.form.sharesSum', { sum: shown }), warn: false }
}

// ── a number ─────────────────────────────────────────────────────────────────
function numberProblem(f: StatField): string {
  const problem = statNumberProblem(f, props.params[f.key])
  return problem ? t(`stat.form.${problem}`, { above: f.above ?? '', below: f.below ?? '' }) : ''
}
const defaultOf = (f: StatField) => {
  const d = test.value?.defaults[f.key]
  return d === null || d === undefined ? '' : String(d)
}

// ── a choice ─────────────────────────────────────────────────────────────────
const chosen = (f: StatField) => props.params[f.key] ?? test.value?.defaults[f.key]
function forbidden(f: StatField, value: unknown): string {
  const rule = test.value ? statForbidden(test.value, f.key, value, props.params) : null
  if (!rule) return ''
  const key = statReasonKey(rule.reason)
  return te(key) ? t(key) : rule.message
}
</script>

<template>
  <div v-if="test" class="statform">
    <p class="about">{{ $t(statHintKey(test)) }}</p>
    <p class="note" role="status" aria-live="polite"><template v-if="repairedNote"><Info :size="13" aria-hidden="true" /> {{ repairedNote }}</template></p>

    <!-- columns being resolved: a skeleton in place of the false "no column" -->
    <div v-if="columnsLoading && !inputColumns.length" class="skcols" aria-busy="true">
      <span class="sk" style="width: 68%" />
      <span class="sk" style="width: 52%" />
      <span class="sk" style="width: 61%" />
    </div>
    <div v-else-if="!inputColumns.length" class="note">
      <Info :size="13" aria-hidden="true" /> {{ $t('paramForm.noUpstreamColumns') }}
    </div>

    <template v-for="f in fields" :key="f.key">
      <!-- a column of the step's input -->
      <div v-if="f.control === 'column'" class="field">
        <span :id="idOf(f.key, 'label')" class="lbl">{{ labelOf(f) }}</span>
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
        <p v-if="noUsableColumn(f)" class="note warn"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('stat.form.noNumericColumns') }}</p>
        <p v-else-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- some of a column's values: the groups to compare, in their order -->
      <div v-else-if="f.control === 'levels'" class="field" role="group" :aria-labelledby="idOf(f.key, 'label')">
        <span :id="idOf(f.key, 'label')" class="lbl">{{ labelOf(f) }}</span>
        <span v-if="levels[f.key]?.state === 'loading'" class="sk" style="width: 130px" aria-busy="true" />
        <template v-else-if="levels[f.key]?.state === 'done'">
          <div v-for="(_, place) in (draft[f.key] ?? [])" :key="place" class="place">
            <span class="placen" aria-hidden="true">{{ place + 1 }}</span>
            <Select
              :model-value="draft[f.key]?.[place] ?? ''"
              :options="levelOptions(f, place)"
              searchable
              :aria-label="`${labelOf(f)}: ${$t('stat.form.level', { n: place + 1 })}`"
              :aria-describedby="hintOf(f) ? idOf(f.key, 'hint') : undefined"
              @update:model-value="(v: any) => setLevel(f, place, v)"
            />
          </div>
        </template>
        <p v-if="levelNote(f)" class="note" role="status"><Info :size="13" aria-hidden="true" /> {{ levelNote(f) }}</p>
        <p v-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- one value of a column: chosen among its values, or written -->
      <div v-else-if="f.control === 'level'" class="field">
        <label :for="idOf(f.key)" class="lbl">{{ labelOf(f) }}</label>
        <input
          :id="idOf(f.key)"
          type="text"
          autocomplete="off"
          :list="idOf(f.key, 'values')"
          :value="shownValue(f)"
          :placeholder="$t('stat.form.successPlaceholder')"
          :aria-describedby="hintOf(f) ? idOf(f.key, 'hint') : undefined"
          @change="set(f.key, ($event.target as HTMLInputElement).value)"
        />
        <datalist :id="idOf(f.key, 'values')" :aria-label="$t('stat.form.successValues', { column: columnFor(f) })">
          <option v-for="l in labelsOf(f)" :key="l" :value="l" />
        </datalist>
        <p v-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- a proportion for each value of a column, or all equal -->
      <fieldset v-else-if="f.control === 'proportions'" class="field choices">
        <legend class="lbl">{{ labelOf(f) }}</legend>
        <label class="chk">
          <input type="radio" :name="idOf(f.key)" :checked="!sharesGiven(f)" @change="chooseShares(f, false)" />
          <span>{{ $t('stat.form.equalShares') }}</span>
        </label>
        <label class="chk">
          <input type="radio" :name="idOf(f.key)" :checked="sharesGiven(f)" @change="chooseShares(f, true)" />
          <span>{{ $t('stat.form.givenShares') }}</span>
        </label>
        <template v-if="sharesGiven(f)">
          <p v-if="!columnFor(f)" class="note" role="status"><Info :size="13" aria-hidden="true" /> {{ levelNote(f) }}</p>
          <span v-else-if="levels[f.key]?.state === 'loading'" class="sk" style="width: 130px" aria-busy="true" />
          <template v-else>
            <div v-for="(l, i) in shareLabels(f)" :key="l" class="place share">
              <label :for="idOf(f.key, `share-${i}`)" class="sharel" :title="l">{{ l }}</label>
              <input
                :id="idOf(f.key, `share-${i}`)"
                type="number"
                step="any"
                min="0"
                max="1"
                :value="sharesOf(f)[l] ?? ''"
                :aria-label="$t('stat.form.shareOf', { level: l })"
                @change="setShare(f, l, ($event.target as HTMLInputElement).value)"
              />
              <span v-if="!labelsOf(f).includes(l)" class="why warn">{{ $t('stat.form.notInData') }}</span>
            </div>
            <p class="note" :class="{ warn: sharesNote(f).warn }" role="status">
              <TriangleAlert v-if="sharesNote(f).warn" :size="13" aria-hidden="true" /> {{ sharesNote(f).text }}
            </p>
          </template>
        </template>
        <p v-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
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

      <!-- a number: written when the box is left, as every field of the panel -->
      <div v-else-if="f.control === 'number'" class="field">
        <label :for="idOf(f.key)" class="lbl">{{ labelOf(f) }}</label>
        <input
          :id="idOf(f.key)"
          type="number"
          step="any"
          :value="params[f.key] ?? ''"
          :placeholder="defaultOf(f)"
          :aria-invalid="numberProblem(f) ? 'true' : undefined"
          :aria-describedby="numberProblem(f) ? idOf(f.key, 'problem') : hintOf(f) ? idOf(f.key, 'hint') : undefined"
          @change="set(f.key, ($event.target as HTMLInputElement).value)"
        />
        <p v-if="numberProblem(f)" :id="idOf(f.key, 'problem')" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ numberProblem(f) }}</p>
        <p v-else-if="hintOf(f)" :id="idOf(f.key, 'hint')" class="hint">{{ hintOf(f) }}</p>
      </div>

      <!-- one of a few choices: native radio buttons, the arrows move among them -->
      <fieldset v-else-if="f.control === 'choice'" class="field choices">
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
            {{ $t(`stat.choices.${f.kind}.${c}`) }}
            <span v-if="forbidden(f, c)" class="why" :class="{ warn: chosen(f) === c }">{{ forbidden(f, c) }}</span>
          </span>
        </label>
        <p v-if="hintOf(f)" class="hint">{{ hintOf(f) }}</p>
      </fieldset>
    </template>
  </div>
</template>

<style scoped>
.statform { display: flex; flex-direction: column; gap: 12px; }
.about { margin: 0; font-size: 12px; line-height: 1.45; color: var(--muted); }
.skcols { display: flex; flex-direction: column; gap: 8px; padding: 2px 0; }
.field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
fieldset.field { border: none; margin: 0; padding: 0; }
.lbl, legend.lbl { font-size: 12px; color: var(--muted); padding: 0; }
legend.lbl { margin-bottom: 4px; }
.hint { margin: 0; font-size: 11.5px; line-height: 1.4; color: var(--muted); }
.note { margin: 0; font-size: 12px; line-height: 1.4; color: var(--muted); display: flex; align-items: flex-start; gap: 5px; }
.note svg { flex-shrink: 0; margin-top: 2px; }
.note.warn, .why.warn { color: var(--warning); }
.chk { display: flex; align-items: flex-start; gap: 6px; color: var(--text); font-size: 13px; }
.chk input { width: auto; flex-shrink: 0; margin-top: 2px; }
.chk.off { color: var(--muted); }
.why { display: block; font-size: 11.5px; color: var(--muted); }
.place { display: flex; align-items: center; gap: 6px; }
.place > :deep(.sel-trigger) { flex: 1; min-width: 0; }
.placen { flex-shrink: 0; width: 14px; text-align: right; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.share { gap: 8px; }
.sharel { flex: 1; min-width: 0; font-size: 12.5px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.share input { width: 96px; flex-shrink: 0; }
</style>
