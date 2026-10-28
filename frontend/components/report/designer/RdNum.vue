<script setup lang="ts">
// A number of the definition: millimetres to a tenth, points, a count. The value is
// written when it is a number within its bounds; an empty field is null where null is
// allowed (`nullable`), else it keeps the last good value.
import { ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: number | null | undefined
  unit?: string
  step?: number
  min?: number
  max?: number
  decimals?: number
  nullable?: boolean
  label?: string
  id?: string
  placeholder?: string
}>(), { step: 0.1, decimals: 1, unit: '', min: -Infinity, max: Infinity, label: undefined, id: undefined, placeholder: '' })
const emit = defineEmits<{ (e: 'update:modelValue', v: number | null): void }>()

const shown = (v: number | null | undefined) => (v == null ? '' : String(Math.round(v * 10 ** props.decimals) / 10 ** props.decimals))
const text = ref(shown(props.modelValue))
watch(() => props.modelValue, (v) => {
  if (Number(text.value.replace(',', '.')) !== v) text.value = shown(v)
})

function onInput(e: Event) {
  text.value = (e.target as HTMLInputElement).value
  const raw = text.value.trim().replace(',', '.')
  if (raw === '') {
    if (props.nullable) emit('update:modelValue', null)
    return
  }
  const n = Number(raw)
  if (!Number.isFinite(n) || n < props.min || n > props.max) return
  emit('update:modelValue', Math.round(n * 10 ** props.decimals) / 10 ** props.decimals)
}
function onBlur() {
  text.value = shown(props.modelValue)
}
</script>

<template>
  <span class="rnum">
    <input
      :id="id"
      type="text"
      inputmode="decimal"
      :value="text"
      :aria-label="label"
      :placeholder="placeholder"
      @input="onInput"
      @blur="onBlur"
      @keydown.up.prevent="emit('update:modelValue', Math.min(max, Math.round(((modelValue ?? 0) + step) * 10 ** decimals) / 10 ** decimals))"
      @keydown.down.prevent="emit('update:modelValue', Math.max(min, Math.round(((modelValue ?? 0) - step) * 10 ** decimals) / 10 ** decimals))"
    />
    <span v-if="unit" class="rnum-unit" aria-hidden="true">{{ unit }}</span>
  </span>
</template>

<style scoped>
.rnum { position: relative; display: flex; min-width: 0; }
.rnum input { padding: 5px 8px; font-variant-numeric: tabular-nums; min-width: 0; }
.rnum-unit { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); font-size: 11px; color: var(--muted); pointer-events: none; }
.rnum:has(.rnum-unit) input { padding-right: 30px; }
</style>
