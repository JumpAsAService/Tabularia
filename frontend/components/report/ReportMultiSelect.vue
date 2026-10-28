<script setup lang="ts">
// A parameter that takes several of its choices: a field like the Select, which opens a
// list of checkboxes (with a search when the list is long) and «All» / «None». Not modal:
// Esc or a click outside closes it and the focus goes back to the field.
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDown, Search } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: string[]
  options: { value: string; label: string }[]
  placeholder?: string
  invalid?: boolean
  describedby?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string[]): void }>()
defineOptions({ inheritAttrs: false })

const { t } = useI18n()
const open = ref(false)
const query = ref('')
const root = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)

// open: a press outside, or the focus leaving the whole field, closes it; Esc too, and
// gives the focus back to the field
function onDown(e: MouseEvent) {
  if (!root.value?.contains(e.target as Node)) close()
}
function onKey(e: KeyboardEvent) {
  if (e.key !== 'Escape' || !open.value) return
  e.preventDefault()
  e.stopPropagation()
  close()
  trigger.value?.focus()
}
function onFocusOut(e: FocusEvent) {
  const to = e.relatedTarget as Node | null
  if (to && !root.value?.contains(to)) close()
}
function close() {
  if (!open.value) return
  open.value = false
  document.removeEventListener('mousedown', onDown)
}
onBeforeUnmount(close)

const chosen = computed(() => new Set(props.modelValue))
const shown = computed(() => {
  const q = query.value.trim().toLowerCase()
  return q ? props.options.filter((o) => o.label.toLowerCase().includes(q)) : props.options
})
const summary = computed(() => {
  const labels = props.options.filter((o) => chosen.value.has(o.value)).map((o) => o.label)
  // a value the list no longer has (a link from before) is still shown
  const extra = props.modelValue.filter((v) => !props.options.some((o) => o.value === v))
  return [...labels, ...extra].join(', ')
})

function toggle(v: string) {
  emit('update:modelValue', chosen.value.has(v) ? props.modelValue.filter((x) => x !== v) : [...props.modelValue, v])
}
function all() { emit('update:modelValue', props.options.map((o) => o.value)) }
function none() { emit('update:modelValue', []) }
function flip() {
  if (open.value) return close()
  open.value = true
  query.value = ''
  document.addEventListener('mousedown', onDown)
  nextTick(() => panel.value?.querySelector<HTMLElement>('input')?.focus())
}
const uid = Math.random().toString(36).slice(2, 9)
</script>

<template>
  <div ref="root" class="ms" @focusout="onFocusOut" @keydown="onKey">
    <button
      ref="trigger"
      type="button"
      class="ms-trigger"
      :class="{ open, invalid }"
      v-bind="$attrs"
      aria-haspopup="true"
      :aria-expanded="open"
      :aria-controls="open ? `ms-${uid}` : undefined"
      :aria-describedby="describedby"
      :aria-invalid="invalid ? 'true' : undefined"
      :title="summary || undefined"
      @click="flip"
    >
      <span class="ms-label" :class="{ ph: !summary }">{{ summary || placeholder || '—' }}</span>
      <span v-if="modelValue.length > 1" class="ms-count">{{ modelValue.length }}</span>
      <ChevronDown :size="13" class="chev" :class="{ flip: open }" />
    </button>
    <div v-if="open" :id="`ms-${uid}`" ref="panel" class="ms-panel">
      <label v-if="options.length > 8" class="ms-search">
        <Search :size="13" aria-hidden="true" />
        <input v-model="query" type="text" :placeholder="t('select.search')" :aria-label="t('select.search')" />
      </label>
      <div class="ms-list" role="group" :aria-label="String($attrs['aria-label'] ?? '')">
        <label v-for="o in shown" :key="o.value" class="ms-opt">
          <input type="checkbox" :checked="chosen.has(o.value)" @change="toggle(o.value)" />
          <span>{{ o.label }}</span>
        </label>
        <p v-if="!shown.length" class="ms-empty">{{ options.length ? t('select.noResults') : t('select.noOptions') }}</p>
      </div>
      <div class="ms-foot">
        <button type="button" class="mini" @click="all">{{ t('report.chooseAll') }}</button>
        <button type="button" class="mini" :disabled="!modelValue.length" @click="none">{{ t('report.chooseNone') }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ms { position: relative; min-width: 0; }
.ms-trigger {
  width: 100%; display: flex; align-items: center; gap: 6px; justify-content: space-between;
  background: var(--bg-soft); border: 1px solid var(--control-border); border-radius: 8px;
  padding: 6px 10px; text-align: left; font: inherit; color: var(--text);
}
.ms-trigger:hover { border-color: var(--accent); box-shadow: none; }
.ms-trigger:active { transform: none; }
.ms-trigger.open { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
.ms-trigger.invalid { border-color: var(--danger); }
.ms-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ms-label.ph { color: var(--muted); }
.ms-count {
  flex-shrink: 0; font-size: 11px; line-height: 16px; min-width: 18px; padding: 0 5px; border-radius: 999px; text-align: center;
  background: var(--tint-accent); color: var(--accent-hi); font-variant-numeric: tabular-nums;
}
.chev { color: var(--muted); flex-shrink: 0; transition: transform 0.15s; }
.chev.flip { transform: rotate(180deg); }
.ms-panel {
  position: absolute; z-index: 60; top: calc(100% + 4px); left: 0; min-width: 100%; max-width: 340px;
  display: flex; flex-direction: column; background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; box-shadow: var(--shadow-2); padding: 4px;
}
.ms-search { display: flex; align-items: center; gap: 6px; padding: 4px 6px 6px; border-bottom: 1px solid var(--border); margin-bottom: 4px; color: var(--muted); }
.ms-search input { flex: 1; min-width: 0; border: 0; background: transparent; padding: 2px 0; outline: none; box-shadow: none; }
.ms-list { max-height: 240px; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; }
.ms-opt { display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-radius: 6px; font-size: 13px; cursor: pointer; white-space: nowrap; }
.ms-opt:hover { background: var(--panel-2); }
.ms-opt input { width: auto; margin: 0; accent-color: var(--accent); }
.ms-opt span { overflow: hidden; text-overflow: ellipsis; }
.ms-empty { margin: 6px 8px; font-size: 12px; color: var(--muted); }
.ms-foot { display: flex; gap: 6px; padding: 6px 4px 2px; border-top: 1px solid var(--border-soft); margin-top: 4px; }
</style>
