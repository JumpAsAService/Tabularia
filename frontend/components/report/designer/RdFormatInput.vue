<script setup lang="ts">
// A format (`#,##0.00`, `0.0%`, `d MMM yyyy`…): written by hand, or picked among the
// common ones. The report's language sets the separators and the names of the months.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Select from '~/components/ui/Select.vue'
import { useDesign } from '~/composables/reportDesignStore'

const props = defineProps<{ modelValue: string | null | undefined; label: string; item: string | null; path: string }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string | null): void }>()
const store = useDesign()
const { t } = useI18n()
const uid = `rff-${Math.random().toString(36).slice(2, 9)}`

const PRESETS = [
  ['#,##0', '1,235'], ['#,##0.00', '1,234.50'], ['0.0%', '12.3%'], ['0%', '12%'], ['€ #,##0.00', '€ 1,234.50'],
  ['#,##0;(#,##0)', '(1,235)'], ['yyyy-MM-dd', '2026-10-28'], ['dd/MM/yyyy', '28/10/2026'], ['d MMM yyyy', '28 Oct 2026'],
  ['MMMM yyyy', 'October 2026'], ['HH:mm', '14:05'],
]
const options = computed(() => PRESETS.map(([value, example]) => ({ value, label: value, hint: t('reportDesigner.props.formatExample', { example }) })))
const problem = computed(() => store.problemAt(props.item, props.path))

function set(v: string | null) {
  emit('update:modelValue', v && v.trim() ? v : null)
  store.clearProblem(props.item, props.path)
}
</script>

<template>
  <div class="rff" :data-path="path">
    <label :for="uid" class="rff-label">{{ label }}</label>
    <div class="rff-row">
      <input :id="uid" type="text" class="rff-input" :value="modelValue ?? ''" spellcheck="false" maxlength="64"
             :placeholder="$t('reportDesigner.props.formatNone')" :aria-invalid="!!problem" @input="set(($event.target as HTMLInputElement).value)" />
      <Select class="rff-presets" :model-value="null" :options="options" :placeholder="$t('reportDesigner.props.formatPresets')"
              :aria-label="$t('reportDesigner.props.formatPresets')" @update:model-value="set" />
    </div>
    <p v-if="problem" class="rff-err" role="alert">{{ problem.message }}</p>
  </div>
</template>

<style scoped>
.rff { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.rff-label { font-size: 12px; color: var(--muted); font-weight: 500; }
.rff-row { display: flex; gap: 6px; min-width: 0; }
.rff-input { flex: 1; min-width: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; padding: 5px 8px; }
.rff-input[aria-invalid='true'] { border-color: var(--danger); }
.rff-row :deep(.rff-presets) { width: 96px; flex: none; font-size: 12px; padding: 5px 8px; }
.rff-err { margin: 0; font-size: 12px; color: var(--danger); }
</style>
