<script setup lang="ts">
// A list of commands anchored to a point: the «⋯» button of a row, or where the right
// click landed. Not the editor's radial ring (FlowQuickMenu.vue): here the choices are
// commands read as a list, so they stand as one, in a fixed order.
//
// The app's anchored menus' manners: it grows from the corner nearest the anchor (`pop`),
// not at all when opened from the keyboard; focus goes to the first item, ↑ ↓ Home End move,
// Esc and Tab close and give the focus back to whoever opened it, a click outside closes.
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

export interface RowMenuItem {
  id: string
  label: string
  icon?: any
  danger?: boolean
  disabled?: boolean
  busy?: boolean
  /** a separator above this item */
  sep?: boolean
}

const props = defineProps<{
  open: boolean
  /** the anchor, in viewport pixels */
  x: number
  y: number
  /** 'end': the menu's right edge on the anchor (under a button at the row's end) */
  align?: 'start' | 'end'
  items: RowMenuItem[]
  /** opened from the keyboard: no motion */
  byKey?: boolean
  label?: string
}>()
const emit = defineEmits<{ (e: 'select', id: string): void; (e: 'close'): void }>()

const el = ref<HTMLElement | null>(null)
const pos = ref({ left: 0, top: 0, origin: 'top left' })
let origin: HTMLElement | null = null

const MARGIN = 8

function place() {
  const m = el.value
  if (!m) return
  const w = m.offsetWidth
  const h = m.offsetHeight
  const vw = window.innerWidth
  const vh = window.innerHeight
  let left = props.align === 'end' ? props.x - w : props.x
  let top = props.y
  let ox = props.align === 'end' ? 'right' : 'left'
  let oy = 'top'
  // past the right or bottom edge: it opens the other way from the anchor
  if (left + w > vw - MARGIN) { left = props.x - w; ox = 'right' }
  if (left < MARGIN) { left = Math.min(MARGIN, vw - w); ox = 'left' }
  if (top + h > vh - MARGIN) { top = Math.max(MARGIN, props.y - h); oy = 'bottom' }
  pos.value = { left, top, origin: `${oy} ${ox}` }
}

const itemsEls = () => Array.from(el.value?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? [])

function focusAt(i: number) {
  const list = itemsEls()
  if (!list.length) return
  list[(i + list.length) % list.length].focus()
}

function onKey(e: KeyboardEvent) {
  const list = itemsEls()
  const at = list.indexOf(document.activeElement as HTMLElement)
  if (e.key === 'ArrowDown') { e.preventDefault(); focusAt(at + 1) }
  else if (e.key === 'ArrowUp') { e.preventDefault(); focusAt(at < 0 ? -1 : at - 1) }
  else if (e.key === 'Home') { e.preventDefault(); focusAt(0) }
  else if (e.key === 'End') { e.preventDefault(); focusAt(-1) }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); emit('close') }
  else if (e.key === 'Tab') { e.preventDefault(); emit('close') }
}

function pick(it: RowMenuItem) {
  if (it.disabled) return
  // the focus goes back first: the command may move it on (a rename field, a dialog)
  origin?.focus?.({ preventScroll: true })
  origin = null
  emit('select', it.id)
}

function onDocDown(e: MouseEvent) {
  if (el.value && !el.value.contains(e.target as Node)) emit('close')
}

watch(() => props.open, async (now) => {
  if (now) {
    origin = document.activeElement as HTMLElement | null
    await nextTick()
    place()
    focusAt(0)
    document.addEventListener('mousedown', onDocDown, true)
    window.addEventListener('resize', close, { passive: true })
    window.addEventListener('scroll', close, { passive: true, capture: true })
  } else {
    unlisten()
    origin?.focus?.({ preventScroll: true })
    origin = null
  }
})
function close() { emit('close') }
function unlisten() {
  document.removeEventListener('mousedown', onDocDown, true)
  window.removeEventListener('resize', close)
  window.removeEventListener('scroll', close, true)
}
onBeforeUnmount(unlisten)

const style = computed(() => ({ left: `${pos.value.left}px`, top: `${pos.value.top}px`, transformOrigin: pos.value.origin }))
</script>

<template>
  <Teleport to="body">
    <Transition name="pop" :css="!byKey">
      <div v-if="open" ref="el" class="rowmenu" role="menu" :aria-label="label" :style="style" @keydown="onKey" @contextmenu.prevent>
        <template v-for="it in items" :key="it.id">
          <div v-if="it.sep" class="rm-sep" role="separator" />
          <button
            type="button"
            role="menuitem"
            tabindex="-1"
            class="rm-item"
            :class="{ danger: it.danger }"
            :aria-label="it.label"
            :aria-disabled="it.disabled ? 'true' : undefined"
            :aria-busy="it.busy ? 'true' : undefined"
            @click="pick(it)"
          >
            <component :is="it.icon" v-if="it.icon" :size="14" class="rm-icon" />
            <span>{{ it.label }}</span>
          </button>
        </template>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.rowmenu {
  position: fixed;
  z-index: 1500;
  min-width: 210px;
  padding: 5px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
}
.rm-item {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 9px;
  width: 100%;
  min-height: 30px;
  padding: 5px 9px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--text);
  font-size: 13px;
  text-align: left;
  white-space: nowrap;
}
.rm-item:hover:not([aria-disabled="true"]),
.rm-item:focus-visible,
.rm-item:focus { background: var(--panel-2); box-shadow: none; outline: none; }
.rm-item[aria-disabled="true"] { opacity: 0.5; cursor: not-allowed; }
.rm-icon { color: var(--muted); flex-shrink: 0; transition: color var(--dur-tint); }
.rm-item:hover:not([aria-disabled="true"]) .rm-icon { color: var(--text); }
.rm-item.danger, .rm-item.danger .rm-icon { color: var(--danger-text); }
.rm-item.danger:hover:not([aria-disabled="true"]),
.rm-item.danger:focus { background: color-mix(in srgb, var(--danger) 12%, transparent); }
.rm-sep { height: 1px; margin: 4px 6px; background: var(--border-soft); }
</style>
