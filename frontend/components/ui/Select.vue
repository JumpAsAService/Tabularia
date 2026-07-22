<script setup lang="ts">
// Select custom in tema: il popup delle <select> native è disegnato dall'OS e
// non è stilizzabile — questo componente lo sostituisce ovunque. Il pannello è
// teleportato sul body (position: fixed) così non viene mai tagliato dagli
// overflow dei contenitori (pannello nodo, toolbar, celle di tabella).
import { ref, computed, nextTick, onBeforeUnmount } from 'vue'
import { ChevronDown, Check, Search } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

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
  // casella di ricerca: default automatico quando le opzioni sono molte
  searchable?: boolean
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

// ── ricerca: attiva su richiesta o quando l'elenco è lungo ────────────────
const SEARCH_THRESHOLD = 8
const query = ref('')
const searchable = computed(() => props.searchable ?? norm.value.length > SEARCH_THRESHOLD)
const filtered = computed<SelectOption[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return norm.value
  return norm.value.filter((o) => labelOf(o).toLowerCase().includes(q))
})

// opzioni (filtrate) raggruppate preservando l'ordine (group undefined = fuori gruppo)
const groups = computed(() => {
  const out: { group: string | null; items: SelectOption[] }[] = []
  for (const o of filtered.value) {
    const g = o.group ?? null
    const last = out[out.length - 1]
    if (last && last.group === g) last.items.push(o)
    else out.push({ group: g, items: [o] })
  }
  return out
})

const open = ref(false)
const hi = ref(-1) // indice evidenziato (navigazione tastiera), sul flat filtrato
const triggerEl = ref<HTMLElement | null>(null)
const panelEl = ref<HTMLElement | null>(null)
const searchEl = ref<HTMLInputElement | null>(null)
const panelStyle = ref<Record<string, string>>({})

const PANEL_MAX_H = 280

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
// lo scroll DENTRO il pannello non deve chiuderlo: solo lo scroll della pagina
// sottostante (il pannello è position:fixed e non seguirebbe) lo chiude.
function onScroll(ev: Event) {
  const t = ev.target as Node
  if (panelEl.value && t && panelEl.value.contains(t)) return
  close()
}
const closeOnMove = () => close()

async function openPanel() {
  if (props.disabled) return
  open.value = true
  query.value = ''
  hi.value = norm.value.findIndex((o) => o.value === props.modelValue)
  await nextTick()
  positionPanel()
  if (searchable.value) searchEl.value?.focus()
  // scroll fino all'opzione selezionata
  panelEl.value?.querySelector('.sel-opt.active')?.scrollIntoView({ block: 'nearest' })
  document.addEventListener('mousedown', onDocMousedown)
  window.addEventListener('scroll', onScroll, true)
  window.addEventListener('resize', closeOnMove)
}

function close() {
  if (!open.value) return
  open.value = false
  document.removeEventListener('mousedown', onDocMousedown)
  window.removeEventListener('scroll', onScroll, true)
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
  const flat = filtered.value
  if (ev.key === 'Escape') { ev.preventDefault(); close() }
  else if (ev.key === 'ArrowDown') {
    ev.preventDefault()
    hi.value = Math.min(hi.value + 1, flat.length - 1)
    scrollHiIntoView()
  } else if (ev.key === 'ArrowUp') {
    ev.preventDefault()
    hi.value = Math.max(hi.value - 1, 0)
    scrollHiIntoView()
  } else if (ev.key === 'Enter') { ev.preventDefault(); if (flat[hi.value]) pick(flat[hi.value]) }
}

// il testo di ricerca è cambiato: riporta l'evidenziazione in cima ai risultati
function onQueryInput() {
  hi.value = filtered.value.length ? 0 : -1
}

async function scrollHiIntoView() {
  await nextTick()
  panelEl.value?.querySelector('.sel-opt.hi')?.scrollIntoView({ block: 'nearest' })
}

const flatIndex = (o: SelectOption) => filtered.value.indexOf(o)
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
      <div v-if="searchable" class="sel-search">
        <Search :size="13" class="sel-search-icon" />
        <input
          ref="searchEl"
          v-model="query"
          type="text"
          class="sel-search-input"
          :placeholder="t('select.search')"
          @input="onQueryInput"
          @keydown="onKeydown"
        />
      </div>
      <div class="sel-list">
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
        <p v-if="!norm.length" class="sel-empty">{{ $t('select.noOptions') }}</p>
        <p v-else-if="!filtered.length" class="sel-empty">{{ $t('select.noResults') }}</p>
      </div>
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
  max-height: 280px;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow-2);
  padding: 4px;
}
/* la casella di ricerca resta fissa; solo la lista scrolla */
.sel-search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px 6px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 4px;
  flex-shrink: 0;
}
.sel-search-icon { color: var(--muted); flex-shrink: 0; }
.sel-search-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  font: inherit;
  font-size: 13px;
  color: var(--text);
}
.sel-search-input::placeholder { color: var(--muted); }
.sel-list {
  overflow-y: auto;
  min-height: 0;
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
