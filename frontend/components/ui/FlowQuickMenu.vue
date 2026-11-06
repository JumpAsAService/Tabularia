<script setup lang="ts">
// The flow editor's right-click menu: a ring of the person's transformations around the
// click, the first at 12 o'clock and the others clockwise, «Customize» in the hub.
// The editor (FlowEditor.vue) opens it with `show`, creates the node on `pick`, and opens
// the customizing dialog on `customize`. The node is born at the click even when the ring
// moves inwards to stay inside the canvas.
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { SlidersHorizontal } from 'lucide-vue-next'
import { opMeta } from '~/composables/useOpIcons'
import {
  QUICK_MENU_RADIUS, radialItemOffset, radialLabelSide, radialMenuCenter, radialMenuExtent,
  type QuickMenuBox,
} from '~/composables/flowShortcuts'

const props = defineProps<{ items: string[] }>()
const emit = defineEmits<{
  (e: 'pick', op: string): void
  (e: 'customize'): void
  (e: 'close', reason: 'escape' | 'outside' | 'other'): void
}>()
const { t } = useI18n()

const CLOSE_MS = 100
// a menu opened again this soon after closing comes up without its entrance: it is
// opened tens of times a day, and the second time in a row it has nothing to show
const REOPEN_MS = 300

const visible = ref(false)
const phase = ref<'open' | 'closing' | 'picking'>('open')
const run = ref(0) // a new key at every opening: the entrance starts over, nothing half-way
const center = ref({ x: 0, y: 0 })
const active = ref(0)
const picked = ref(-1)
const root = ref<HTMLElement | null>(null)
// by position on the ring (a v-for ref array does not promise the items' order)
const itemEls: HTMLButtonElement[] = []
const labelEls: HTMLElement[] = []
let timer: ReturnType<typeof setTimeout> | null = null
let closedAt = -Infinity
// no entrance and no exit: opened from the keyboard (Menu key, Shift+F10) or again at once
const still = ref(false)
// opened by the pointer: the focus moves without its ring (the hover already shows the
// item) until a key is pressed, so two items never look current at once
let quietFocus = false

const ring = computed(() => props.items.map((op, i) => {
  const at = radialItemOffset(i, props.items.length)
  return { op, i, x: at.x, y: at.y, side: radialLabelSide(at.angle), meta: opMeta(op) }
}))
const circumference = 2 * Math.PI * QUICK_MENU_RADIUS

function clearTimer() {
  if (timer) clearTimeout(timer)
  timer = null
}

/** Opens at `click` (viewport px), kept inside `bounds` (the canvas). `keyboard`: opened
 *  from the keyboard, it appears without motion. */
async function show(click: { x: number; y: number }, bounds: QuickMenuBox, keyboard = false) {
  clearTimer()
  still.value = keyboard || performance.now() - closedAt < REOPEN_MS
  quietFocus = !keyboard
  picked.value = -1
  phase.value = 'open'
  active.value = 0
  center.value = { ...click }
  run.value++
  itemEls.length = 0
  labelEls.length = 0
  visible.value = true
  await nextTick()
  // the labels are laid out (invisible) from the start: their sizes say how far the open
  // menu reaches, and the ring moves inwards before the first frame is painted
  const sizes = props.items.map((_, i) => ({ width: labelEls[i]?.offsetWidth ?? 0, height: labelEls[i]?.offsetHeight ?? 0 }))
  center.value = radialMenuCenter(click, radialMenuExtent(props.items.length, sizes), bounds)
  focusItem(0)
  // the right button's press may still hand the focus to the canvas after this: the ring
  // takes it back on the next frame
  const opened = run.value
  requestAnimationFrame(() => {
    if (run.value === opened && phase.value === 'open' && !root.value?.contains(document.activeElement)) focusItem(active.value)
  })
}

// the exit: 100ms of fade, or none for a menu that came up without motion
function leave(to: 'closing' | 'picking') {
  phase.value = to
  closedAt = performance.now()
  clearTimer()
  if (still.value) {
    visible.value = false
    return
  }
  timer = setTimeout(() => { visible.value = false; timer = null }, CLOSE_MS)
}

function hide(reason: 'escape' | 'outside' | 'other' = 'other') {
  if (!visible.value || phase.value !== 'open') return
  leave('closing')
  emit('close', reason)
}

function choose(i: number) {
  if (phase.value !== 'open') return
  const op = props.items[i]
  if (!op) return
  picked.value = i
  // the node is born at once; the chosen item's pulse plays while the menu fades
  leave('picking')
  emit('pick', op)
}

function customize() {
  if (phase.value !== 'open') return
  leave('closing')
  emit('customize')
}

function focusEl(el: HTMLElement | null | undefined) {
  el?.focus({ focusVisible: !quietFocus } as FocusOptions)
}

function focusHub() {
  focusEl(root.value?.querySelector<HTMLElement>('.qm-hub'))
}

function focusItem(i: number) {
  if (!props.items.length) {
    focusHub()
    return
  }
  active.value = (i + props.items.length) % props.items.length
  focusEl(itemEls[active.value])
}

// the mouse over an item (or the hub) takes the focus there: the ring's current item is
// the one under the pointer, and Enter acts on what is lit
function onPointerMove(ev: PointerEvent, i: number | null) {
  if (ev.pointerType !== 'mouse' || phase.value !== 'open' || document.activeElement === ev.currentTarget) return
  quietFocus = true
  if (i === null) focusHub()
  else focusItem(i)
}

function onKey(ev: KeyboardEvent) {
  // the editor's own shortcuts (Delete removes the selected node…) do not act under the menu
  ev.stopPropagation()
  quietFocus = false // from the first key on, the focus shows its ring
  const onHub = (ev.target as HTMLElement | null)?.classList.contains('qm-hub')
  switch (ev.key) {
    case 'Escape': ev.preventDefault(); hide('escape'); return
    case 'ArrowRight': case 'ArrowDown': ev.preventDefault(); focusItem(active.value + 1); return
    case 'ArrowLeft': case 'ArrowUp': ev.preventDefault(); focusItem(active.value - 1); return
    case 'Home': ev.preventDefault(); focusItem(0); return
    case 'End': ev.preventDefault(); focusItem(props.items.length - 1); return
    case 'Tab':
      // the menu keeps the focus: the ring's current item ↔ the hub
      ev.preventDefault()
      if (onHub) focusItem(active.value)
      else focusHub()
      return
    case 'Enter': case ' ':
      ev.preventDefault()
      if (onHub) customize()
      else choose(active.value)
  }
}

// a press anywhere else closes it (the canvas' own handlers still see the press)
function onDocDown(ev: PointerEvent) {
  if (visible.value && root.value && !root.value.contains(ev.target as globalThis.Node)) hide('outside')
}
document.addEventListener('pointerdown', onDocDown, true)
window.addEventListener('resize', onResize)
function onResize() { hide() }
onBeforeUnmount(() => {
  clearTimer()
  document.removeEventListener('pointerdown', onDocDown, true)
  window.removeEventListener('resize', onResize)
})

defineExpose({ show, hide, isOpen: () => visible.value && phase.value === 'open' })
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      :key="run"
      ref="root"
      class="qm"
      :class="[phase, { still }]"
      role="menu"
      :aria-label="t('quickMenu.menuLabel')"
      :style="{ left: `${center.x}px`, top: `${center.y}px` }"
      @keydown="onKey"
      @contextmenu.prevent
    >
      <svg class="qm-guide" aria-hidden="true" :width="QUICK_MENU_RADIUS * 2 + 2" :height="QUICK_MENU_RADIUS * 2 + 2"
        :viewBox="`${-QUICK_MENU_RADIUS - 1} ${-QUICK_MENU_RADIUS - 1} ${QUICK_MENU_RADIUS * 2 + 2} ${QUICK_MENU_RADIUS * 2 + 2}`">
        <circle :r="QUICK_MENU_RADIUS" cx="0" cy="0" :style="{ '--qm-c': `${circumference}` }" />
      </svg>
      <button
        v-for="it in ring"
        :key="it.op"
        :ref="(el) => { if (el) itemEls[it.i] = el as HTMLButtonElement }"
        type="button"
        class="qm-item"
        :class="[`qm-${it.side}`, { picked: picked === it.i }]"
        role="menuitem"
        :tabindex="active === it.i ? 0 : -1"
        :aria-label="t(it.meta.label)"
        :style="{ '--x': `${it.x}px`, '--y': `${it.y}px`, '--i': it.i, '--c': it.meta.color }"
        @click="choose(it.i)"
        @focus="active = it.i"
        @pointermove="onPointerMove($event, it.i)"
      >
        <component :is="it.meta.icon" :size="18" aria-hidden="true" />
        <span :ref="(el) => { if (el) labelEls[it.i] = el as HTMLElement }" class="qm-label" aria-hidden="true">{{ t(it.meta.label) }}</span>
      </button>
      <button
        type="button"
        class="qm-hub"
        role="menuitem"
        :aria-label="t('quickMenu.customize')"
        :title="t('quickMenu.customize')"
        @click="customize"
        @pointermove="onPointerMove($event, null)"
      >
        <SlidersHorizontal :size="14" aria-hidden="true" />
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
/* The root is a zero-size point at the menu's centre: everything is placed around it. */
.qm {
  position: fixed; z-index: 1500; width: 0; height: 0;
  transition: opacity var(--dur-1) var(--ease-out), scale var(--dur-1) var(--ease-out);
}
.qm.closing { opacity: 0; scale: 0.96; pointer-events: none; }
.qm.picking { pointer-events: none; }
.qm.picking .qm-guide, .qm.picking .qm-hub, .qm.picking .qm-item:not(.picked) {
  opacity: 0; transition: opacity var(--dur-1) var(--ease-out);
}

.qm-guide { position: absolute; left: 0; top: 0; translate: -50% -50%; overflow: visible; pointer-events: none; }
.qm-guide circle {
  fill: none; stroke: var(--border-soft); stroke-width: 1;
  /* drawn clockwise from 12 o'clock: a circle's path starts at 3 */
  transform: rotate(-90deg);
  stroke-dasharray: var(--qm-c);
  animation: qm-draw 180ms var(--ease-out) backwards;
}

.qm-hub, .qm-item {
  position: absolute; display: grid; place-items: center; padding: 0;
  border-radius: 50%; border: 1px solid var(--border); cursor: pointer;
  font: inherit; line-height: 1;
}
.qm-hub {
  left: -15px; top: -15px; width: 30px; height: 30px;
  background: var(--panel-2); color: var(--muted);
  animation: qm-hub var(--dur-1) var(--ease-out) backwards;
}
.qm-hub:hover, .qm-hub:focus { border-color: var(--accent); color: var(--text); scale: 1.08; }

.qm-item {
  left: calc(var(--x) - 20px); top: calc(var(--y) - 20px); width: 40px; height: 40px;
  background: var(--panel); color: var(--c); box-shadow: var(--shadow-2);
  /* out of the centre one after the other, clockwise from 12 o'clock */
  animation: qm-out 140ms var(--ease-out) calc(var(--i) * 12ms) backwards;
}
.qm-item:hover, .qm-item:focus { border-color: var(--accent); scale: 1.08; z-index: 1; }
.qm-item.picked { animation: qm-pulse 120ms var(--ease-out); }
.qm-hub:focus-visible, .qm-item:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* the name, outwards along the item's radius: never over the other items */
.qm-label {
  position: absolute; white-space: nowrap; pointer-events: none;
  padding: 3px 9px; border-radius: 999px; font-size: 12px; line-height: 16px;
  background: var(--panel-2); color: var(--text); border: 1px solid var(--border-soft); box-shadow: var(--shadow-1);
  opacity: 0;
}
.qm-right .qm-label { left: calc(100% + 8px); top: 50%; translate: -4px -50%; }
.qm-left .qm-label { right: calc(100% + 8px); top: 50%; translate: 4px -50%; }
.qm-top .qm-label { bottom: calc(100% + 8px); left: 50%; translate: -50% 4px; }
.qm-bottom .qm-label { top: calc(100% + 8px); left: 50%; translate: -50% -4px; }
.qm-item:hover .qm-label, .qm-item:focus .qm-label { opacity: 1; }
.qm-right:hover .qm-label, .qm-right:focus .qm-label,
.qm-left:hover .qm-label, .qm-left:focus .qm-label { translate: 0 -50%; }
.qm-top:hover .qm-label, .qm-top:focus .qm-label,
.qm-bottom:hover .qm-label, .qm-bottom:focus .qm-label { translate: -50% 0; }

/* the pointer's hover eases in; the arrow keys move the focus at once */
@media (hover: hover) and (pointer: fine) {
  .qm-hub, .qm-item { transition: border-color 120ms var(--ease-out), color 120ms var(--ease-out), scale 120ms var(--ease-out); }
  .qm-label { transition: opacity 120ms var(--ease-out), translate 120ms var(--ease-out); }
}
.qm-item:focus-visible, .qm-item:focus-visible .qm-label, .qm-hub:focus-visible { transition-duration: 0ms; }

/* opened from the keyboard, or again at once: there, without motion */
.qm.still, .qm.still * { animation: none !important; transition: none !important; }

@keyframes qm-hub { from { opacity: 0; transform: scale(0.9); } }
@keyframes qm-draw { from { stroke-dashoffset: var(--qm-c); } }
@keyframes qm-out { from { opacity: 0; transform: translate(calc(-1 * var(--x)), calc(-1 * var(--y))) scale(0.6); } }
@keyframes qm-pulse { 50% { transform: scale(1.12); } }
@keyframes qm-fade { from { opacity: 0; } }

@media (prefers-reduced-motion: reduce) {
  .qm-guide circle { animation: none; }
  .qm-hub, .qm-item { animation: qm-fade var(--dur-1) var(--ease-out) backwards; }
  .qm-item.picked { animation: none; }
  .qm.closing { scale: 1; }
  .qm-label, .qm-item, .qm-hub { transition-property: opacity, border-color, color; }
  .qm-item:hover, .qm-item:focus, .qm-hub:hover, .qm-hub:focus { scale: 1; }
}
</style>
