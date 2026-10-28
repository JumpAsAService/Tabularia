<script setup lang="ts">
// A colour of the report as #rrggbb: the swatch opens the browser's picker, the field takes
// it written; empty is «none» (the theme's, or no fill) when `clearable`.
import { ref, watch } from 'vue'
import { X } from 'lucide-vue-next'

const props = defineProps<{ modelValue: string | null | undefined; label?: string; clearable?: boolean; placeholder?: string }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string | null): void }>()

const text = ref(props.modelValue ?? '')
watch(() => props.modelValue, (v) => { if ((v ?? '') !== text.value) text.value = v ?? '' })

function onText(e: Event) {
  text.value = (e.target as HTMLInputElement).value.trim()
  if (/^#[0-9a-fA-F]{6}$/.test(text.value)) emit('update:modelValue', text.value.toLowerCase())
  else if (!text.value && props.clearable) emit('update:modelValue', null)
}
</script>

<template>
  <span class="rcol">
    <label class="rcol-swatch" :style="{ background: modelValue || 'transparent' }" :class="{ none: !modelValue }">
      <input type="color" :value="modelValue || '#1f2933'" :aria-label="label" @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)" />
    </label>
    <input type="text" class="rcol-text" :value="text" maxlength="7" spellcheck="false" :placeholder="placeholder ?? '#rrggbb'" :aria-label="label" @input="onText" @blur="text = modelValue ?? ''" />
    <button v-if="clearable && modelValue" type="button" class="rcol-x" :aria-label="$t('reportDesigner.props.clearColor')" :title="$t('reportDesigner.props.clearColor')" @click="emit('update:modelValue', null)">
      <X :size="12" />
    </button>
  </span>
</template>

<style scoped>
.rcol { position: relative; display: flex; align-items: center; gap: 6px; min-width: 0; }
.rcol-swatch {
  position: relative; flex: none; width: 26px; height: 26px; border-radius: 6px; border: 1px solid var(--control-border);
  cursor: pointer; overflow: hidden;
}
.rcol-swatch.none { background: repeating-linear-gradient(45deg, var(--bg-soft) 0 4px, var(--panel-2) 4px 8px) !important; }
.rcol-swatch input { position: absolute; inset: 0; opacity: 0; width: 100%; height: 100%; cursor: pointer; border: 0; padding: 0; }
.rcol-swatch:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
.rcol-text { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; padding: 5px 8px; min-width: 0; }
.rcol-x { position: absolute; right: 4px; padding: 3px; border: 0; background: transparent; color: var(--muted); min-height: 0; box-shadow: none; }
.rcol-x:hover { color: var(--text); box-shadow: none; }
</style>
