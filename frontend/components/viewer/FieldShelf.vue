<script setup lang="ts">
// One of the three areas the Viewer's table is built on: Rows, Columns, Measures. Fields
// are dropped on it (from the list, from the table's header, from another area) and stay as
// chips: dragged to reorder or to another area, removed with × or Del, and a measure's
// function chosen on its chip. Empty, it says its name and what to drop. The same drop
// language as the report designer: a dashed outline while a field is in the air, the accent
// under the pointer, a line where the field would land.
import { computed, nextTick, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { X } from 'lucide-vue-next'
import { draggedField, endFieldDrag, fieldDragOf, fieldDragOutside, startFieldDrag, type FieldDragPayload } from '~/composables/fieldDrag'
import { insertionSlot, type ShelfArea } from '~/composables/viewerColumns'

export interface ShelfChip { name: string; dtype: string; func?: string; unused?: boolean }

const props = withDefaults(defineProps<{
  area: ShelfArea
  title: string
  hint: string
  chips: ShelfChip[]
  // chips one under the other (Rows) instead of side by side
  vertical?: boolean
  // a measure's functions: the chip carries a picker
  funcs?: { value: string; label: string }[]
  // said under the chips (the cross table's one measure)
  note?: string
}>(), { vertical: false, funcs: undefined, note: undefined })
const emit = defineEmits<{
  (e: 'place', payload: FieldDragPayload, at: number): void
  (e: 'remove', index: number): void
  (e: 'shift', index: number, delta: number): void
  (e: 'send', index: number, to: ShelfArea): void
  (e: 'func', index: number, func: string): void
}>()
const { t } = useI18n()

const box = ref<HTMLElement | null>(null)
const over = ref(false)
const slot = ref(-1) // where the dragged field would land
const armed = computed(() => !!draggedField.value)

function chipEls(): HTMLElement[] {
  return [...(box.value?.querySelectorAll<HTMLElement>('.fs-chip') ?? [])]
}
function slotAt(e: DragEvent): number {
  const edges = chipEls().map((el) => {
    const r = el.getBoundingClientRect()
    return props.vertical ? { left: r.top, right: r.bottom } : { left: r.left, right: r.right }
  })
  return insertionSlot(edges, props.vertical ? e.clientY : e.clientX)
}
function onOver(e: DragEvent) {
  const p = fieldDragOf(e)
  if (!p) return
  e.preventDefault()
  e.stopPropagation()
  fieldDragOutside.value = false
  if (e.dataTransfer) e.dataTransfer.dropEffect = p.from === 'list' ? 'copy' : 'move'
  over.value = true
  const at = slotAt(e)
  // its own place, or the one after it: nothing would move
  slot.value = p.from === props.area && p.index !== undefined && (at === p.index || at === p.index + 1) ? -1 : at
}
function onLeave(e: DragEvent) {
  const to = e.relatedTarget as Node | null
  if (!to || !box.value?.contains(to)) { over.value = false; slot.value = -1 }
}
function onDrop(e: DragEvent) {
  const p = fieldDragOf(e)
  over.value = false
  if (!p) return
  e.preventDefault()
  e.stopPropagation()
  const at = slotAt(e)
  slot.value = -1
  endFieldDrag()
  emit('place', p, at)
}
function onChipDrag(e: DragEvent, i: number) {
  const c = props.chips[i]
  startFieldDrag(e, { name: c.name, dtype: c.dtype, from: props.area, index: i, func: c.func })
}

// the keyboard: one tab stop an area, the arrows along its chips
const at = ref(0)
function focusChip(i: number) {
  const n = props.chips.length
  if (!n) return
  at.value = Math.max(0, Math.min(n - 1, i))
  nextTick(() => box.value?.querySelectorAll<HTMLElement>('.fs-name')[at.value]?.focus({ preventScroll: true }))
}
function onKey(e: KeyboardEvent, i: number) {
  if (e.altKey) return
  const mod = e.ctrlKey || e.metaKey
  const back = props.vertical ? 'ArrowUp' : 'ArrowLeft'
  const fwd = props.vertical ? 'ArrowDown' : 'ArrowRight'
  const done = () => { e.preventDefault(); e.stopPropagation() }
  if (e.key === back || e.key === fwd) {
    if (mod) return
    done()
    const d = e.key === back ? -1 : 1
    // Shift with the arrow moves the chip, the arrow alone the focus
    if (e.shiftKey) {
      if (i + d < 0 || i + d >= props.chips.length) return
      emit('shift', i, d)
      focusChip(i + d)
    } else focusChip(i + d)
    return
  }
  // Del, or Ctrl/⌘+Backspace where there is no Del
  if ((e.key === 'Delete' && !mod) || (e.key === 'Backspace' && mod)) {
    done()
    emit('remove', i)
    focusChip(Math.min(i, props.chips.length - 2))
    return
  }
  if (mod || e.shiftKey) return
  const to: Record<string, ShelfArea> = { r: 'rows', c: 'cols', m: 'measures' }
  if (to[e.key] && to[e.key] !== props.area) { done(); emit('send', i, to[e.key]) }
}
defineExpose({ focusChip })
</script>

<template>
  <section
    ref="box"
    class="fs"
    :class="[`fs-${area}`, { vertical, armed, over, empty: !chips.length }]"
    :aria-label="title"
    @dragover="onOver"
    @dragleave="onLeave"
    @drop="onDrop"
  >
    <h3 class="fs-title"><slot name="icon" /> {{ title }}</h3>
    <p v-if="!chips.length" class="fs-hint">{{ hint }}</p>
    <ul v-else class="fs-chips">
      <template v-for="(c, i) in chips" :key="`${c.name}\u0001${c.func ?? ''}`">
        <li v-if="slot === i" class="fs-ins" aria-hidden="true" />
        <li class="fs-chip" :class="{ lift: draggedField?.from === area && draggedField.index === i, unused: c.unused }">
          <Select
            v-if="funcs && c.func"
            :model-value="c.func"
            :options="funcs"
            class="fs-func"
            :aria-label="t('viewer.measureFunc', { name: c.name })"
            @update:model-value="emit('func', i, String($event))"
          />
          <button
            type="button"
            class="fs-name"
            draggable="true"
            :tabindex="i === Math.min(at, chips.length - 1) ? 0 : -1"
            :title="c.unused ? t('viewer.measureUnused', { name: c.name }) : c.name"
            :aria-describedby="`fs-keys-${area}`"
            @dragstart="onChipDrag($event, i)"
            @dragend="endFieldDrag()"
            @focus="at = i"
            @keydown="onKey($event, i)"
          >{{ c.name }}</button>
          <button type="button" class="fs-x" tabindex="-1" :aria-label="t('viewer.chipRemove', { name: c.name, area: title })" :title="t('viewer.chipRemove', { name: c.name, area: title })" @click="emit('remove', i)">
            <X :size="12" aria-hidden="true" />
          </button>
        </li>
      </template>
      <li v-if="slot === chips.length" class="fs-ins" aria-hidden="true" />
    </ul>
    <p v-if="note && chips.length" class="fs-note">{{ note }}</p>
    <span :id="`fs-keys-${area}`" class="sr-only">{{ t('viewer.chipKeysHint') }}</span>
  </section>
</template>

<style scoped>
/* an area is a region of the result panel, ruled off from the others: not a card */
.fs {
  position: relative; display: flex; align-items: center; gap: 8px; min-width: 0; padding: 7px 10px;
  outline: 1.5px dashed transparent; outline-offset: -4px; border-radius: 2px;
  transition: outline-color var(--dur-1) ease-out, background-color var(--dur-1) ease-out;
}
.fs.vertical { flex-direction: column; align-items: stretch; gap: 6px; padding: 9px 8px; }
.fs.armed { outline-color: var(--control-border); }
.fs.over { outline-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); }
.fs-title {
  display: inline-flex; align-items: center; gap: 5px; flex: none; margin: 0;
  font-size: 10.5px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); white-space: nowrap;
}
.fs-hint { margin: 0; font-size: 12px; line-height: 1.4; color: var(--muted); min-width: 0; }
.fs:not(.vertical) .fs-hint { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fs-note { margin: 0; font-size: 11.5px; color: var(--muted); flex: none; }
.fs-chips { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; list-style: none; margin: 0; padding: 0; min-width: 0; }
.fs.vertical .fs-chips { flex-direction: column; flex-wrap: nowrap; align-items: stretch; overflow-y: auto; min-height: 0; }
.fs-chip {
  display: inline-flex; align-items: center; min-width: 0; max-width: 100%; height: 26px;
  border: 1px solid var(--control-border); border-radius: 6px; background: var(--panel-2);
}
.fs-chip.lift { opacity: 0.42; }
.fs-chip.unused { border-style: dashed; }
.fs-chip.unused .fs-name { color: var(--muted); }
.fs-name {
  display: block; flex: 1; min-width: 0; height: 100%; padding: 0 4px 0 8px; border: 0; background: none; box-shadow: none; border-radius: 5px 0 0 5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; text-align: left; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: grab; touch-action: manipulation;
}
.fs-name:hover { background: none; box-shadow: none; }
.fs-name:active { transform: none; cursor: grabbing; }
.fs-name:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.fs-x {
  flex: none; display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 100%; padding: 0;
  border: 0; background: none; box-shadow: none; color: var(--muted); border-radius: 0 5px 5px 0;
}
.fs-x:hover { color: var(--danger); background: none; box-shadow: none; }
.fs-x:active { transform: none; }
/* a measure's function, before its field: «sum · ricavo» */
.fs-func.sel-trigger {
  flex: none; width: auto; height: 24px; min-height: 0; padding: 0 5px 0 7px; gap: 3px;
  border: 0; border-right: 1px solid var(--control-border); border-radius: 5px 0 0 5px;
  background: transparent; box-shadow: none; font-size: 11.5px; color: var(--accent-hi, var(--accent));
}
.fs-func.sel-trigger:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
/* where the dragged field would land: a line in the accent between two chips */
.fs-ins { flex: none; align-self: stretch; width: 2px; min-height: 22px; border-radius: 1px; background: var(--accent); list-style: none; }
.fs.vertical .fs-ins { width: auto; height: 2px; min-height: 0; }
@media (pointer: coarse) {
  .fs-chip { height: 34px; }
  .fs-x { width: 32px; }
}
@media (prefers-reduced-motion: reduce) { .fs { transition: none; } }
</style>
