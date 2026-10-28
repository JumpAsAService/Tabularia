<script setup lang="ts">
// The values of a report's parameters in a Pushmail cell: a compact form built from the
// report's parameters (GET /reports/{id}/parameters), two fields to a line. The values
// are texts, as the cell keeps them: empty is the report's default when the email
// leaves, and a date may be a placeholder resolved then ({start_of_month-1m}), so the
// dates are text fields here, not the browser's date picker. The gateway checks the
// values when the group is saved and again when it runs. A value for a parameter the
// report no longer has is dropped, saying so: the save would refuse it.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { LoaderCircle, TriangleAlert } from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import ReportMultiSelect from '~/components/report/ReportMultiSelect.vue'
import { errMessage } from '~/composables/useApi'
import { useReports, type ReportParameter } from '~/composables/useReports'
import type { PushmailReportParams } from '~/composables/useFlowGroups'

const props = defineProps<{
  reportId: number
  modelValue: PushmailReportParams
  /** unique on the page: the fields' ids */
  idPrefix: string
  /** a reader who may not change the cell: nothing is dropped */
  readOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: PushmailReportParams): void }>()

const { t } = useI18n()
const api = useReports()
const params = ref<ReportParameter[]>([])
const loading = ref(false)
const error = ref('')
const dropped = ref<string[]>([])

let asked = 0
async function load() {
  const mine = ++asked
  loading.value = true
  error.value = ''
  dropped.value = []
  try {
    // the defaults and the choices as the report's form shows them, without the cell's
    // values: one the report no longer takes would make the gateway refuse the list
    const got = await api.parameters(props.reportId)
    if (mine !== asked) return
    params.value = got
    const known = new Set(got.map((p) => p.name))
    dropped.value = Object.keys(props.modelValue).filter((name) => !known.has(name))
    if (dropped.value.length && !props.readOnly) {
      emit('update:modelValue', Object.fromEntries(Object.entries(props.modelValue).filter(([name]) => known.has(name))))
    }
  } catch (e) {
    if (mine === asked) {
      params.value = []
      error.value = errMessage(e)
    }
  } finally {
    if (mine === asked) loading.value = false
  }
}
watch(() => props.reportId, load, { immediate: true })

const fieldId = (p: ReportParameter) => `${props.idPrefix}-p-${p.name}`
function set(name: string, v: string | string[]) {
  const next = { ...props.modelValue }
  if ((Array.isArray(v) && !v.length) || v === '') delete next[name]
  else next[name] = v
  emit('update:modelValue', next)
}
const text = (name: string) => {
  const v = props.modelValue[name]
  return Array.isArray(v) ? v.join(', ') : v ?? ''
}
const list = (name: string) => {
  const v = props.modelValue[name]
  return Array.isArray(v) ? v : v ? [v] : []
}

/** The default as it reads today, for the empty field. */
function shownDefault(p: ReportParameter): string {
  const d = p.default
  if (d === null || d === undefined || (Array.isArray(d) && !d.length)) return ''
  if (p.type === 'boolean') return d ? t('report.yes') : t('report.no')
  const label = (v: unknown) => p.choices?.find((c) => String(c.value) === String(v))?.label ?? String(v)
  return Array.isArray(d) ? d.map(label).join(', ') : label(d)
}
const placeholder = (p: ReportParameter) => {
  const d = shownDefault(p)
  return d ? t('pushmail.paramDefault', { value: d }) : (p.required ? '' : t('report.anyValue'))
}
// the first option of a list: no value of its own, the report's default when it leaves
const defaultOption = (p: ReportParameter) => ({
  value: '',
  label: shownDefault(p) ? t('pushmail.paramDefault', { value: shownDefault(p) }) : t('report.anyValue'),
})
const canBeEmpty = (p: ReportParameter) => !p.required || !!shownDefault(p)

type Control = 'multi' | 'choice' | 'boolean' | 'text'
function control(p: ReportParameter): Control {
  if (p.choices && !p.choices_truncated) return p.multi ? 'multi' : 'choice'
  if (p.type === 'boolean' && !p.multi) return 'boolean'
  return 'text'
}
const choiceOptions = (p: ReportParameter) => (p.choices ?? []).map((c) => ({ value: String(c.value), label: c.label ?? String(c.value) }))
const listOptions = (p: ReportParameter) => {
  const base = control(p) === 'boolean'
    ? [{ value: 'true', label: t('report.yes') }, { value: 'false', label: t('report.no') }]
    : choiceOptions(p)
  return canBeEmpty(p) ? [defaultOption(p), ...base] : base
}
// a field of several values without a list: typed with commas
function typed(p: ReportParameter, raw: string) {
  set(p.name, p.multi ? raw.split(',').map((x) => x.trim()).filter(Boolean) : raw)
}
const anyDate = computed(() => params.value.some((p) => p.type === 'date'))
// braces in a message are its parameters: the example comes in as one
const DATE_EXAMPLE = '{start_of_month-1m}'
</script>

<template>
  <div class="rpp">
    <p v-if="loading && !params.length" class="muted line"><LoaderCircle :size="12" class="spin" /> {{ t('pushmail.paramsLoading') }}</p>
    <p v-else-if="error" class="line warn" role="alert"><TriangleAlert :size="12" /> {{ t('pushmail.paramsError', { error }) }}</p>
    <p v-else-if="!params.length" class="muted line">{{ t('pushmail.paramsNone') }}</p>
    <div v-else class="rpp-fields">
      <div v-for="p in params" :key="p.name" class="pf">
        <label class="pl" :for="fieldId(p)">
          <span class="pname" :title="p.label">{{ p.label }}</span>
          <span v-if="p.required" class="req" aria-hidden="true">*</span>
        </label>
        <ReportMultiSelect
          v-if="control(p) === 'multi'"
          :id="fieldId(p)"
          :model-value="list(p.name)"
          :options="choiceOptions(p)"
          :placeholder="placeholder(p)"
          :aria-label="p.label"
          @update:model-value="(v: string[]) => set(p.name, v)"
        />
        <Select
          v-else-if="control(p) !== 'text'"
          :id="fieldId(p)"
          :model-value="text(p.name)"
          :options="listOptions(p)"
          :placeholder="placeholder(p) || t('pushmail.paramChoose')"
          :aria-label="p.label"
          @update:model-value="(v: string) => set(p.name, v)"
        />
        <input
          v-else
          :id="fieldId(p)"
          type="text"
          :inputmode="p.type === 'number' ? 'decimal' : undefined"
          :value="text(p.name)"
          :placeholder="placeholder(p)"
          :class="{ mono: p.type === 'date' }"
          autocomplete="off"
          spellcheck="false"
          @input="typed(p, ($event.target as HTMLInputElement).value)"
        />
        <p v-if="p.choices_error" class="pmsg warn" :title="t('report.choicesError', { error: p.choices_error })">
          <TriangleAlert :size="11" /> <span class="clamp">{{ t('report.choicesError', { error: p.choices_error }) }}</span>
        </p>
        <p v-else-if="p.multi && control(p) === 'text'" class="pmsg muted">{{ t('report.multiHint') }}</p>
      </div>
    </div>
    <p v-if="dropped.length && !readOnly" class="line warn" role="status">
      <TriangleAlert :size="12" /> {{ t('pushmail.paramsDropped', { names: dropped.join(', ') }, dropped.length) }}
    </p>
    <p v-if="params.length" class="muted line hint">
      {{ t('pushmail.paramsHint') }}<template v-if="anyDate"> {{ t('pushmail.paramsDateHint', { example: DATE_EXAMPLE }) }}</template>
    </p>
  </div>
</template>

<style scoped>
.rpp { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.rpp-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 10px; }
.pf { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.pl { display: flex; align-items: baseline; gap: 3px; font-size: 12px; color: var(--muted); min-width: 0; }
.pname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.req { color: var(--accent-hi); font-weight: 700; }
.pf input { width: 100%; box-sizing: border-box; min-width: 0; }
.pf input.mono { font-family: ui-monospace, monospace; font-size: 12.5px; }
.line { margin: 0; font-size: 11.5px; display: flex; align-items: center; gap: 5px; }
.line.hint { display: block; line-height: 1.45; }
.line.warn, .pmsg.warn { color: var(--warning); }
.pmsg { margin: 0; font-size: 11px; display: flex; align-items: center; gap: 4px; min-width: 0; }
.pmsg svg { flex-shrink: 0; }
@media (max-width: 520px) { .rpp-fields { grid-template-columns: minmax(0, 1fr); } }
.clamp { display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
</style>
