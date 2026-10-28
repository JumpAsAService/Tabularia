<script setup lang="ts">
// A choice among a few, side by side (alignment, orientation, kind of source): a group of
// radio buttons drawn as one control. Arrows move the choice, as in a radio group.

const props = defineProps<{
  modelValue: unknown
  options: { value: unknown; label: string; icon?: unknown; title?: string }[]
  label: string
  iconsOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: any): void }>()

function key(e: KeyboardEvent) {
  const i = props.options.findIndex((o) => o.value === props.modelValue)
  const d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0
  if (!d) return
  e.preventDefault()
  const next = props.options[(i + d + props.options.length) % props.options.length]
  emit('update:modelValue', next.value)
  const host = (e.currentTarget as HTMLElement)
  requestAnimationFrame(() => host.querySelector<HTMLElement>('[aria-checked="true"]')?.focus())
}
</script>

<template>
  <div class="rseg" role="radiogroup" :aria-label="label" @keydown="key">
    <button
      v-for="o in options"
      :key="String(o.value)"
      type="button"
      role="radio"
      :aria-checked="o.value === modelValue"
      :tabindex="o.value === modelValue || (modelValue == null && o === options[0]) ? 0 : -1"
      :class="{ on: o.value === modelValue, icon: iconsOnly }"
      :title="o.title ?? (iconsOnly ? o.label : undefined)"
      :aria-label="iconsOnly ? o.label : undefined"
      @click="emit('update:modelValue', o.value)"
    >
      <component :is="o.icon" v-if="o.icon" :size="14" />
      <span v-if="!iconsOnly">{{ o.label }}</span>
    </button>
  </div>
</template>

<style scoped>
.rseg { display: inline-flex; padding: 2px; gap: 2px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--control-border); min-width: 0; }
.rseg button {
  flex: 1; border: 0; background: transparent; padding: 4px 9px; font-size: 12px; color: var(--muted); border-radius: 6px;
  box-shadow: none; white-space: nowrap; min-width: 0;
}
.rseg button.icon { padding: 4px 7px; }
.rseg button:hover { color: var(--text); box-shadow: none; }
.rseg button.on { background: var(--panel-2); color: var(--text); box-shadow: var(--shadow-1); }
.rseg button:active { transform: none; }
</style>
