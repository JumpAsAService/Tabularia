<script setup lang="ts">
// A parameter of the report, added or changed: its name (what the SQL writes as {name} and
// the expressions as PARAM("name")), its label in the form, its type, one value or many,
// whether it must be given, its default (a value, or a placeholder of Tabularia's language
// such as {today-30}), and its choices — fixed, or read from a dataset.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from '~/components/ui/Modal.vue'
import Select from '~/components/ui/Select.vue'
import RdExprInput from '~/components/report/designer/RdExprInput.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import { useDesign } from '~/composables/reportDesignStore'
import { NAME_RE, PARAM_TYPES, RESERVED_PARAMS, type DParameter, type ParamType } from '~/composables/reportDesignModel'

const props = defineProps<{ open: boolean; name: string | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const store = useDesign()
const { t } = useI18n()

interface Draft {
  name: string
  label: string
  type: ParamType
  multi: boolean
  required: boolean
  defaultText: string
  choices: 'none' | 'values' | 'dataset'
  valuesText: string
  dataset: string | null
  value: string
  labelExpr: string
}
const draft = ref<Draft | null>(null)
const original = ref<string | null>(null)

const show = (v: unknown) => (v == null ? '' : String(v))
watch(() => props.open, (o) => {
  if (!o) return
  const p = props.name ? store.doc.value.parameters.find((x) => x.name === props.name) ?? null : null
  original.value = p?.name ?? null
  const c = p?.choices ?? null
  draft.value = {
    name: p?.name ?? '',
    label: p?.label ?? '',
    type: p?.type ?? 'text',
    multi: !!p?.multi,
    required: !!p?.required,
    defaultText: Array.isArray(p?.default) ? (p!.default as unknown[]).map(show).join(', ') : show(p?.default),
    choices: c ? ('values' in c ? 'values' : 'dataset') : 'none',
    valuesText: c && 'values' in c ? c.values.map(show).join('\n') : '',
    dataset: c && 'dataset' in c ? c.dataset : null,
    value: c && 'dataset' in c ? c.value : '',
    labelExpr: c && 'dataset' in c ? c.label ?? '' : '',
  }
}, { immediate: true })

const typeOptions = computed(() => PARAM_TYPES.map((v) => ({ value: v, label: t(`reportDesigner.param.types.${v}`) })))
const choiceOptions = computed(() => [
  { value: 'none', label: t('reportDesigner.param.choicesNone') },
  { value: 'values', label: t('reportDesigner.param.choicesValues') },
  { value: 'dataset', label: t('reportDesigner.param.choicesDataset') },
])
const datasetOptions = computed(() => store.doc.value.datasets.map((d) => ({ value: d.name, label: d.name })))
const choiceFields = computed(() => store.fieldsOf(draft.value?.dataset))

/** A value written in the form, as the definition keeps it: a number for a number, true
 *  or false for a boolean; a placeholder stays text, resolved at every render. */
function convert(text: string, type: ParamType): unknown {
  const s = text.trim()
  if (/^\{[^{}]+\}$/.test(s)) return s
  if (type === 'number' && /^[+-]?(\d+(\.\d*)?|\.\d+)$/.test(s)) return Number(s)
  if (type === 'boolean' && /^(true|false)$/i.test(s)) return s.toLowerCase() === 'true'
  return s
}

const nameError = computed(() => {
  const d = draft.value
  if (!d) return ''
  if (!NAME_RE.test(d.name)) return t('reportDesigner.param.nameRule')
  if (RESERVED_PARAMS.has(d.name)) return t('reportDesigner.param.nameReserved', { name: d.name })
  if (d.name !== original.value && store.doc.value.parameters.some((p) => p.name === d.name)) return t('reportDesigner.param.nameTaken', { name: d.name })
  return ''
})
const choiceError = computed(() => {
  const d = draft.value
  if (!d) return ''
  if (d.choices === 'values' && !d.valuesText.trim()) return t('reportDesigner.param.needValues')
  if (d.choices === 'dataset' && (!d.dataset || !d.value.trim())) return t('reportDesigner.param.needDataset')
  return ''
})
const serverProblems = computed(() => (original.value ? store.problems.value.filter((p) => p.parameter === original.value) : []))

function apply() {
  const d = draft.value
  if (!d || nameError.value || choiceError.value) return
  const p: DParameter = { name: d.name, label: d.label.trim(), type: d.type, multi: d.multi, required: d.required }
  const def = d.defaultText.trim()
  if (def) p.default = d.multi && !/^\{[^{}]+\}$/.test(def) ? def.split(',').map((x) => convert(x, d.type)).filter((x) => x !== '') : convert(def, d.type)
  else p.default = null
  if (d.choices === 'values') {
    p.choices = { values: d.valuesText.split('\n').map((x) => convert(x, d.type)).filter((x) => x !== '') }
  } else if (d.choices === 'dataset') {
    p.choices = { dataset: d.dataset!, value: d.value, ...(d.labelExpr.trim() ? { label: d.labelExpr } : {}) }
  }
  const doc = store.doc.value
  const old = original.value
  const at = old ? doc.parameters.findIndex((x) => x.name === old) : -1
  if (at >= 0) doc.parameters.splice(at, 1, p)
  else doc.parameters.push(p)
  if (old && old !== p.name) rename(old, p.name)
  store.problems.value = store.problems.value.filter((x) => x.parameter !== old)
  emit('close')
}

/** A parameter renamed: PARAM("old") in the expressions and {old} in the SQL and the
 *  filters follow it. */
function rename(old: string, name: string) {
  const doc = store.doc.value
  const esc = old.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const text = JSON.stringify({ datasets: doc.datasets, sections: doc.sections })
    .replace(new RegExp(`PARAM\\(\\\\"${esc}\\\\"\\)`, 'g'), `PARAM(\\"${name}\\")`)
    .replace(new RegExp(`(?<!\\{)\\{${esc}\\}(?!\\})`, 'g'), `{${name}}`)
  const back = JSON.parse(text)
  doc.datasets = back.datasets
  doc.sections = back.sections
}
</script>

<template>
  <Modal :open="open" :title="name ? $t('reportDesigner.param.editTitle', { name }) : $t('reportDesigner.param.newTitle')" :width="560" @close="emit('close')">
    <div v-if="draft" class="rpd">
      <div v-for="(p, i) in serverProblems" :key="i" class="rpd-alert" role="alert">{{ p.path ? `${p.path}: ` : '' }}{{ p.message }}</div>
      <div class="rp-row">
        <label class="rp-f">
          <span>{{ $t('reportDesigner.param.name') }}</span>
          <input v-model="draft.name" type="text" maxlength="64" spellcheck="false" class="rpd-mono" :aria-invalid="!!nameError" />
        </label>
        <label class="rp-f">
          <span>{{ $t('reportDesigner.param.label') }}</span>
          <input v-model="draft.label" type="text" maxlength="200" :placeholder="draft.name" />
        </label>
      </div>
      <p v-if="nameError" class="rp-err">{{ nameError }}</p>
      <p v-else-if="draft.name" class="rp-help">{{ $t('reportDesigner.param.usage', { sql: '{' + draft.name + '}', expr: 'PARAM("' + draft.name + '")' }) }}</p>
      <div class="rp-row">
        <label class="rp-f">
          <span>{{ $t('reportDesigner.param.type') }}</span>
          <Select v-model="draft.type" :options="typeOptions" :aria-label="$t('reportDesigner.param.type')" />
        </label>
        <div class="rp-f rpd-checks">
          <label class="rp-check"><input v-model="draft.multi" type="checkbox" /> {{ $t('reportDesigner.param.multi') }}</label>
          <label class="rp-check"><input v-model="draft.required" type="checkbox" /> {{ $t('reportDesigner.param.required') }}</label>
        </div>
      </div>
      <label class="rp-f">
        <span>{{ $t('reportDesigner.param.default') }}</span>
        <input v-model="draft.defaultText" type="text" class="rpd-mono" :placeholder="draft.type === 'date' ? '{today-30}' : draft.multi ? 'a, b' : ''" />
      </label>
      <p class="rp-help">{{ draft.type === 'date' ? $t('reportDesigner.param.defaultDateHelp') : $t('reportDesigner.param.defaultHelp') }}</p>

      <div class="rp-f">
        <span>{{ $t('reportDesigner.param.choices') }}</span>
        <RdSeg v-model="draft.choices" :options="choiceOptions" :label="$t('reportDesigner.param.choices')" />
      </div>
      <template v-if="draft.choices === 'values'">
        <label class="rp-f">
          <span>{{ $t('reportDesigner.param.values') }}</span>
          <textarea v-model="draft.valuesText" rows="4" spellcheck="false" />
        </label>
        <p class="rp-help">{{ $t('reportDesigner.param.valuesHelp') }}</p>
      </template>
      <template v-else-if="draft.choices === 'dataset'">
        <label class="rp-f">
          <span>{{ $t('reportDesigner.props.dataset') }}</span>
          <Select :model-value="draft.dataset" :options="datasetOptions" :placeholder="$t('reportDesigner.props.chooseDataset')" :aria-label="$t('reportDesigner.props.dataset')" @update:model-value="draft.dataset = $event" />
        </label>
        <div class="rp-f">
          <span>{{ $t('reportDesigner.param.choiceValue') }}</span>
          <RdExprInput v-model="draft.value" :fields="choiceFields" :params="[]" :label="$t('reportDesigner.param.choiceValue')" placeholder="[code]" />
        </div>
        <div class="rp-f">
          <span>{{ $t('reportDesigner.param.choiceLabel') }}</span>
          <RdExprInput v-model="draft.labelExpr" :fields="choiceFields" :params="[]" :label="$t('reportDesigner.param.choiceLabel')" placeholder="[name]" />
        </div>
        <p class="rp-help">{{ $t('reportDesigner.param.datasetHelp') }}</p>
      </template>
      <p v-if="choiceError" class="rp-help rpd-need">{{ choiceError }}</p>
    </div>
    <template #footer>
      <span class="rpd-grow" />
      <button type="button" @click="emit('close')">{{ $t('reportDesigner.cancel') }}</button>
      <button type="button" class="primary" :disabled="!!nameError || !!choiceError" @click="apply">{{ name ? $t('reportDesigner.apply') : $t('reportDesigner.param.add') }}</button>
    </template>
  </Modal>
</template>

<style scoped src="~/assets/reportprops.css" />
<style scoped>
.rpd { display: flex; flex-direction: column; gap: 12px; }
.rpd-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.rpd-checks { justify-content: flex-end; gap: 7px; padding-bottom: 2px; }
.rpd-need { color: var(--warning); }
.rpd-alert {
  padding: 8px 10px; border-radius: 8px; font-size: 12px; line-height: 1.4; overflow-wrap: anywhere;
  color: var(--danger); background: color-mix(in srgb, var(--danger) 8%, transparent); border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent);
}
.rpd-grow { flex: 1; }
.rp-f textarea { font-size: 12.5px; }
</style>
