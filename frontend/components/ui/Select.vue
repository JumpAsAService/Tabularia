<script setup lang="ts">
// Select custom in tema: il popup delle <select> native è disegnato dall'OS e
// non è stilizzabile — questo componente lo sostituisce ovunque. Il pannello è
// teleportato sul body (position: fixed) così non viene mai tagliato dagli
// overflow dei contenitori (pannello nodo, toolbar, celle di tabella).
import { ref, computed, nextTick, onBeforeUnmount } from 'vue'
import { ChevronDown, Check } from 'lucide-vue-next'

export interface SelectOption {
  value: any
  label?: string
  group?: string
}

// due root (trigger + Teleport): class/style dei parent vanno esplicitamente sul trigger
defineOptions({ inheritAttrs: false })

const props = defineProps<{
  modelValue: any
  options: Array<SelectOption | string | number>
  placeholder?: string
  disabled?: boolean
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: any): void
  (e: 'close'): void
}>()

const norm = computed<SelectOption[]>(() =>
  props.options.map((o) => (typeof o === 'object' && o !== null ? (o as SelectOption) : { value: o })),
)
const labelOf = (o: SelectOption) => o.label ?? String(o.value)
const selected = computed(() => norm.value.find((o) => o.value === props.modelValue))

// opzioni raggruppate preservando l'ordine (group undefined = fuori gruppo)
const groups = computed(() => {
  const out: { group: string | null; items: SelectOption[] }[] = []
  for (const o of norm.value) {
    const g = o.group ?? null
    const last = out[out.length - 1]
    if (last && last.group === g) last.items.push(o)
    else out.push({ group: g, items: [o] })
  }
  return out
})

const open = ref(false)
const hi = ref(-1) // indice evidenziato (navigazione tastiera), sul flat
const triggerEl = ref<HTMLElement | null>(null)
const panelEl = ref<HTMLElement | null>(null)
const panelStyle = ref<Record<string, string>>({})

const PANEL_MAX_H = 240

function positionPanel() {
  const r = triggerEl.value?.getBoundingClientRect()
  if (!r) return
  const below = window.innerHeight - r.bottom
  const style: Record<string, string> = {
    left: `${r.left}px`,
    minWidth: `${r.width}px`,
    maxWidth: `${Math.max(r.width, 320)}px`,
  }
  if (below < PANEL_MAX_H + 8 && r.top > below) {
    style.bottom = `${window.innerHeight - r.top + 4}px` // si apre verso l'alto
  } else {
    style.top = `${r.bottom + 4}px`
  }
  panelStyle.value = style
}

function onDocMousedown(ev: MouseEvent) {
  const t = ev.target as Node
  if (triggerEl.value?.contains(t) || panelEl.value?.contains(t)) return
  close()
}
const closeOnMove = () => close()

async function openPanel() {
  if (props.disabled) return
  open.value = true
  hi.value = norm.value.findIndex((o) => o.value === props.modelValue)
  await nextTick()
  positionPanel()
  // scroll fino all'opzione selezionata
  panelEl.value?.querySelector('.sel-opt.active')?.scrollIntoView({ block: 'nearest' })
  document.addEventListener('mousedown', onDocMousedown)
  window.addEventListener('scroll', closeOnMove, true)
  window.addEventListener('resize', closeOnMove)
}

function close() {
  if (!open.value) return
  open.value = false
  document.removeEventListener('mousedown', onDocMousedown)
  window.removeEventListener('scroll', closeOnMove, true)
  window.removeEventListener('resize', closeOnMove)
  emit('close')
}
onBeforeUnmount(close)

function toggle() {
  open.value ? close() : openPanel()
}

function pick(o: SelectOption) {
  emit('update:modelValue', o.value)
  close()
}

function onKeydown(ev: KeyboardEvent) {
  if (!open.value) {
    if (['Enter', ' ', 'ArrowDown'].includes(ev.key)) {
      ev.preventDefault()
      openPanel()
    }
    return
  }
  const flat = norm.value
  if (ev.key === 'Escape') { ev.preventDefault(); close() }
  else if (ev.key === 'ArrowDown') { ev.preventDefault(); hi.value = Math.min(hi.value + 1, flat.length - 1) }
  else if (ev.key === 'ArrowUp') { ev.preventDefault(); hi.value = Math.max(hi.value - 1, 0) }
  else if (ev.key === 'Enter') { ev.preventDefault(); if (flat[hi.value]) pick(flat[hi.value]) }
}

const flatIndex = (o: SelectOption) => norm.value.indexOf(o)
</script>

<template>
  <button
    ref="triggerEl"
    type="button"
    class="sel-trigger"
    :class="{ open, disabled }"
    v-bind="$attrs"
    :disabled="disabled"
    @click="toggle"
    @keydown="onKeydown"
  >
    <span class="sel-label" :class="{ ph: !selected }">
      {{ selected ? labelOf(selected) : placeholder ?? '—' }}
    </span>
    <ChevronDown :size="13" class="chev" :class="{ flip: open }" />
  </button>

  <Teleport to="body">
    <div v-if="open" ref="panelEl" class="sel-panel" :style="panelStyle" @mousedown.stop>
      <template v-for="(g, gi) in groups" :key="gi">
        <div v-if="g.group" class="sel-group">{{ g.group }}</div>
        <div
          v-for="o in g.items"
          :key="String(o.value)"
          class="sel-opt"
          :class="{ active: o.value === modelValue, hi: flatIndex(o) === hi }"
          @click="pick(o)"
          @mousemove="hi = flatIndex(o)"
        >
          <Check v-if="o.value === modelValue" :size="13" class="check" />
          <span v-else class="check-spacer" />
          <span class="sel-opt-label">{{ labelOf(o) }}</span>
        </div>
      </template>
      <p v-if="!norm.length" class="sel-empty">No options</p>
    </div>
  </Teleport>
</template>

<style scoped>
.sel-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  width: 100%;
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 10px;
  font: inherit;
  color: var(--text);
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.sel-trigger:hover { border-color: var(--accent); box-shadow: none; transform: none; }
.sel-trigger.open { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(79, 140, 255, 0.18); }
.sel-trigger.disabled { opacity: 0.45; cursor: not-allowed; }
.sel-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sel-label.ph { color: var(--muted); }
.chev { color: var(--muted); flex-shrink: 0; transition: transform 0.15s; }
.chev.flip { transform: rotate(180deg); }
</style>

<style>
/* pannello teleportato sul body: NON scoped. z-index tra i dialog (2000) e i
   toast (3000): un Select dentro un dialog (es. ConnectionDialog) teletrasporta
   il pannello sul body, quindi deve stare SOPRA il backdrop del dialog (non
   dietro), ma sotto le notifiche. */
.sel-panel {
  position: fixed;
  z-index: 2500;
  max-height: 240px;
  overflow-y: auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow-2);
  padding: 4px;
}
.sel-group {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--muted);
  padding: 6px 8px 3px;
}
.sel-opt {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 8px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
  white-space: nowrap;
}
.sel-opt.hi { background: var(--panel-2); }
.sel-opt.active { color: var(--accent-hi); }
.sel-opt .check { color: var(--accent); flex-shrink: 0; }
.sel-opt .check-spacer { width: 13px; flex-shrink: 0; }
.sel-opt-label { overflow: hidden; text-overflow: ellipsis; }
.sel-empty { margin: 6px 8px; font-size: 12px; color: var(--muted); }
</style>
