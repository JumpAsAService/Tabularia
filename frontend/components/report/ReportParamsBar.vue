<script setup lang="ts">
// The report's parameters as a form, in one band above the pages: a field per parameter
// by its type (text, number, date, yes/no), a list when it has choices (several at once
// when it takes several), the required ones marked, and «View report». The values are
// texts, as a link carries them: `''` (or `[]`) is no value, and then the report's default
// holds. The owner keeps them in the address (pages/report.vue).
import { useI18n } from 'vue-i18n'
import { Eye, LoaderCircle, TriangleAlert } from 'lucide-vue-next'
import type { ReportFormValue as FormValue, ReportParameter } from '~/composables/useReports'
import Select from '~/components/ui/Select.vue'
import ReportMultiSelect from '~/components/report/ReportMultiSelect.vue'

const props = defineProps<{
  parameters: ReportParameter[]
  modelValue: Record<string, FormValue>
  /** per parameter: why its value does not fit (required, refused by the server) */
  errors?: Record<string, string>
  busy?: boolean
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: Record<string, FormValue>): void
  (e: 'submit'): void
}>()

const { t } = useI18n()
// one bar per page: the page focuses a field by its id (rp-field-<name>)
const fieldId = (p: ReportParameter) => `rp-field-${p.name}`
const msgId = (p: ReportParameter) => `rp-field-${p.name}-msg`

function set(name: string, v: FormValue) {
  emit('update:modelValue', { ...props.modelValue, [name]: v })
}
const text = (name: string) => {
  const v = props.modelValue[name]
  return Array.isArray(v) ? v.join(', ') : v ?? ''
}
const list = (name: string) => {
  const v = props.modelValue[name]
  return Array.isArray(v) ? v : v ? [v] : []
}

/** The default as a text, for the empty field's placeholder. */
function shownDefault(p: ReportParameter): string {
  const d = p.default
  if (d === null || d === undefined || (Array.isArray(d) && !d.length)) return ''
  if (p.type === 'boolean') return d ? t('report.yes') : t('report.no')
  const label = (v: unknown) => p.choices?.find((c) => String(c.value) === String(v))?.label ?? String(v)
  return Array.isArray(d) ? d.map(label).join(', ') : label(d)
}
// an optional parameter with no default can be left empty: its lists offer «Any»
const canBeEmpty = (p: ReportParameter) => !p.required && (p.default === null || p.default === undefined
  || (Array.isArray(p.default) && !p.default.length))
const placeholder = (p: ReportParameter) => {
  const d = shownDefault(p)
  return d ? t('report.defaultPlaceholder', { value: d }) : (p.required ? '' : t('report.anyValue'))
}

const choiceOptions = (p: ReportParameter) => (p.choices ?? []).map((c) => ({ value: String(c.value), label: c.label ?? String(c.value) }))
const singleOptions = (p: ReportParameter) => [
  ...(canBeEmpty(p) ? [{ value: '', label: t('report.anyValue') }] : []),
  ...choiceOptions(p),
]
const booleanOptions = (p: ReportParameter) => [
  ...(canBeEmpty(p) ? [{ value: '', label: t('report.anyValue') }] : []),
  { value: 'true', label: t('report.yes') },
  { value: 'false', label: t('report.no') },
]
type Control = 'multi' | 'choice' | 'boolean' | 'date' | 'number' | 'text'
function control(p: ReportParameter): Control {
  if (p.choices && !p.choices_truncated) return p.multi ? 'multi' : 'choice'
  if (p.type === 'boolean' && !p.multi) return 'boolean'
  if (p.type === 'date' && !p.multi) return 'date'
  return p.type === 'number' ? 'number' : 'text'
}
function hint(p: ReportParameter): string {
  if (p.choices_error) return ''
  if (p.choices_truncated) return t('report.choicesTruncated')
  if (p.multi && !p.choices) return t('report.multiHint')
  return ''
}
const anyRequired = (ps: ReportParameter[]) => ps.some((p) => p.required)
</script>

<template>
  <form class="pbar" novalidate @submit.prevent="emit('submit')">
    <div v-for="p in parameters" :key="p.name" class="pf" :class="{ invalid: !!errors?.[p.name] }">
      <label class="pl" :for="fieldId(p)">
        <span class="pname">{{ p.label }}</span>
        <span v-if="p.required" class="req" aria-hidden="true">*</span>
      </label>
      <ReportMultiSelect
        v-if="control(p) === 'multi'"
        :id="fieldId(p)"
        :model-value="list(p.name)"
        :options="choiceOptions(p)"
        :placeholder="placeholder(p)"
        :invalid="!!errors?.[p.name]"
        :describedby="msgId(p)"
        :aria-label="p.label"
        :aria-required="p.required ? 'true' : undefined"
        @update:model-value="(v: string[]) => set(p.name, v)"
      />
      <Select
        v-else-if="control(p) === 'choice' || control(p) === 'boolean'"
        :id="fieldId(p)"
        :class="{ invalid: !!errors?.[p.name] }"
        :model-value="text(p.name)"
        :options="control(p) === 'boolean' ? booleanOptions(p) : singleOptions(p)"
        :placeholder="placeholder(p) || '—'"
        :aria-required="p.required ? 'true' : undefined"
        :aria-invalid="errors?.[p.name] ? 'true' : undefined"
        :aria-describedby="msgId(p)"
        @update:model-value="(v: string) => set(p.name, v)"
      />
      <input
        v-else
        :id="fieldId(p)"
        :type="control(p) === 'date' ? 'date' : 'text'"
        :inputmode="control(p) === 'number' ? 'decimal' : undefined"
        :list="p.choices_truncated ? `${fieldId(p)}-list` : undefined"
        :value="text(p.name)"
        :placeholder="placeholder(p)"
        :required="p.required"
        :aria-invalid="errors?.[p.name] ? 'true' : undefined"
        :aria-describedby="msgId(p)"
        autocomplete="off"
        spellcheck="false"
        @input="set(p.name, ($event.target as HTMLInputElement).value)"
      />
      <datalist v-if="p.choices_truncated" :id="`${fieldId(p)}-list`">
        <option v-for="c in choiceOptions(p)" :key="c.value" :value="c.value">{{ c.label }}</option>
      </datalist>
      <p :id="msgId(p)" class="pmsg" :class="{ err: !!errors?.[p.name], warn: !errors?.[p.name] && !!p.choices_error }">
        <template v-if="errors?.[p.name]">{{ errors[p.name] }}</template>
        <template v-else-if="p.choices_error">
          <TriangleAlert :size="12" aria-hidden="true" />
          <span class="clamp" :title="$t('report.choicesError', { error: p.choices_error })">{{ $t('report.choicesError', { error: p.choices_error }) }}</span>
        </template>
        <template v-else>{{ hint(p) }}</template>
      </p>
    </div>
    <div class="pact">
      <button type="submit" class="primary" :disabled="busy">
        <LoaderCircle v-if="busy" :size="14" class="spin" /><Eye v-else :size="14" />
        {{ $t('report.viewReport') }}
      </button>
      <p v-if="anyRequired(parameters)" class="pmsg legend"><span class="req" aria-hidden="true">*</span> {{ $t('report.requiredLegend') }}</p>
    </div>
  </form>
</template>

<style scoped>
.pbar {
  display: flex; flex-wrap: wrap; align-items: flex-start; gap: 4px 14px;
  padding: 10px 14px 4px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--panel);
}
.pf { display: flex; flex-direction: column; gap: 4px; flex: 0 1 210px; min-width: 150px; }
.pl { display: flex; align-items: baseline; gap: 3px; font-size: 12px; font-weight: 500; color: var(--muted); min-width: 0; }
.pname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.req { color: var(--accent-hi); font-weight: 700; }
.pf input { width: 100%; box-sizing: border-box; min-width: 0; }
.pf.invalid input, .pf.invalid :deep(.sel-trigger) { border-color: var(--danger); }
.pmsg {
  margin: 0; min-height: 16px; font-size: 11.5px; line-height: 16px; color: var(--muted);
  display: flex; align-items: center; gap: 4px; overflow-wrap: anywhere;
}
.pmsg.err { color: var(--danger); }
.pmsg.warn { color: var(--warning); }
.pmsg.warn svg { flex-shrink: 0; }
/* the button sits on the fields' line, right after them: the label's height above it */
.pact { display: flex; flex-direction: column; gap: 4px; padding-top: 20px; }
.pact button { white-space: nowrap; flex-shrink: 0; }
/* a long reason (the server's) in two lines; all of it in the tooltip */
.clamp { display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
</style>
