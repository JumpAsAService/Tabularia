<script setup lang="ts">
// The grid being designed, with the data on it: the tiles are the viewer's (DashboardTile), on the
// same rows and gaps (dashboardGrid.ts), so what is seen here is what comes out. The columns share the
// room between the panels; a wider window gives them more, as it does in the viewer.
//
// The gestures are the report designer's: a press moves, a handle resizes, the pointer's way out to
// an edge scrolls. Moving writes on the definition only when a cell is crossed, and always from the
// layout the gesture started with (`pushed`), so what was pushed goes back when the tile has moved on;
// between cells only the tile's own transform changes, and nothing else on the page is touched. The
// whole gesture is one step of the history (the store's `gesture`). The tile's own controls cannot be
// reached here: a click selects it, and it filters nothing.
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Database, EyeOff, Plus, SquareDashedMousePointer } from 'lucide-vue-next'
import DashboardTile from '~/components/dashboard/DashboardTile.vue'
import type { KindCtx } from '~/composables/chartKinds'
import { GRID_COLUMNS, GRID_GAP_PX, GRID_ROW_PX, boxMoved, boxResized, cellsOf, lowestRow, pushed, type GridBox, type GridHandle } from '~/composables/dashboardGrid'
import { placeTile, sizeOfNew, tilesOf, type DTile, type TileKind } from '~/composables/dashboardDesignModel'
import { emptySlots, type TileSlots } from '~/composables/dashboardTiles'
import { DRAG_TYPE, dragged, useDesign } from '~/composables/reportDesignStore'

const props = defineProps<{ slots: TileSlots; ctx: KindCtx; ready: (tile: Record<string, any>) => boolean }>()
const emit = defineEmits<{
  (e: 'add-dataset'): void
  (e: 'insert', kind: TileKind, at?: { x: number; y: number }): void
  (e: 'drop-field', dataset: string, field: { name: string; type: string }, at: { x: number; y: number }, on: string | null): void
  (e: 'said', text: string): void
}>()
const store = useDesign()
const { t } = useI18n()

const STEP_Y = GRID_ROW_PX + GRID_GAP_PX
const HANDLES: GridHandle[] = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w']
const NEW_ROWS_BELOW = 4 // the room under the last tile, to drop into
const LEAST_ROWS = 14

const stage = ref<HTMLElement | null>(null)
const board = ref<HTMLElement | null>(null)
const tiles = computed(() => tilesOf(store.doc.value))
const rows = computed(() => Math.max(LEAST_ROWS, lowestRow(tiles.value) + NEW_ROWS_BELOW))
const boardHeight = computed(() => `${rows.value * STEP_Y - GRID_GAP_PX}px`)
const noData = computed(() => !store.doc.value.datasets.length)

// the order of the page is the reading order; while a tile is being moved the nodes stay where they are
let lastOrder: DTile[] = []
const ordered = computed(() => {
  const all = tiles.value
  if (store.gesture.value && lastOrder.length === all.length) return lastOrder
  lastOrder = [...all].sort((a, b) => a.grid.y - b.grid.y || a.grid.x - b.grid.x)
  return lastOrder
})

const across = `calc((100% + ${GRID_GAP_PX}px) / ${GRID_COLUMNS})`
function boxStyle(g: GridBox) {
  return {
    left: `calc(${across} * ${g.x})`,
    width: `calc(${across} * ${g.w} - ${GRID_GAP_PX}px)`,
    top: `${g.y * STEP_Y}px`,
    height: `${g.h * STEP_Y - GRID_GAP_PX}px`,
  }
}
const entryOf = (id: string) => props.slots[id] ?? emptySlots([id])[id]
const hiddenNow = (id: string) => {
  const a = props.slots[id]?.answer
  return !!a && !('error' in a) && !!a.hidden
}
const nameOf = (tile: DTile) => tile.title || tile.label || tile.id
const kindName = (tile: DTile) => t(`reportDesigner.items.${tile.type === 'table' && tile.matrix ? 'matrix' : tile.type}`)
/** what a tile half made still needs, in the words of what to do */
function needs(tile: DTile): string {
  if (tile.type === 'text') return t('dashboardDesigner.needText')
  if (tile.type === 'image') return t('dashboardDesigner.needImage')
  if (noData.value) return t('dashboardDesigner.needData')
  if (!store.doc.value.datasets.some((d) => d.name === tile.dataset)) return t('dashboardDesigner.needDataset')
  return t(`dashboardDesigner.need_${tile.type}`)
}

// ── gestures ──
interface Gesture {
  kind: 'move' | 'resize'
  id: string
  type: string
  handle?: GridHandle
  start: GridBox
  base: { id: string; type: string; grid: GridBox }[]
  width: number
  sx: number
  sy: number
  scx: number
  scy: number
  moved: boolean
  cols: number
  rows: number
  el: HTMLElement | null
}
let g: Gesture | null = null
const moving = ref<string | null>(null)
const target = ref<GridBox | null>(null)
const badge = ref('')
let lastPointer: PointerEvent | null = null
let edgeTimer = 0

function begin(e: PointerEvent, tile: DTile, kind: 'move' | 'resize', handle?: GridHandle) {
  if (e.button !== 0 || !board.value) return
  e.preventDefault()
  e.stopPropagation()
  g = {
    kind, handle, id: tile.id, type: tile.type, start: { ...tile.grid },
    base: tiles.value.map((x) => ({ id: x.id, type: x.type, grid: { ...x.grid } })),
    width: board.value.clientWidth, sx: e.clientX, sy: e.clientY,
    scx: stage.value?.scrollLeft ?? 0, scy: stage.value?.scrollTop ?? 0, moved: false, cols: 0, rows: 0,
    el: board.value.querySelector<HTMLElement>(`[data-tile="${CSS.escape(tile.id)}"]`),
  }
  stage.value?.focus({ preventScroll: true })
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  window.addEventListener('pointercancel', onUp)
}
function grab(e: PointerEvent, tile: DTile) {
  store.select(tile.id)
  begin(e, tile, 'move')
}

function onMove(e: PointerEvent) {
  if (!g) return
  lastPointer = e
  const dx = e.clientX - g.sx + (stage.value?.scrollLeft ?? 0) - g.scx
  const dy = e.clientY - g.sy + (stage.value?.scrollTop ?? 0) - g.scy
  if (!g.moved) {
    if (Math.hypot(e.clientX - g.sx, e.clientY - g.sy) < 3) return
    g.moved = true
    store.gesture.value = true
    moving.value = g.id
    target.value = g.start
  }
  if (!edgeTimer) edgeTimer = requestAnimationFrame(edgeScroll)
  const cells = cellsOf(dx, dy, g.width)
  if (cells.cols !== g.cols || cells.rows !== g.rows) {
    g.cols = cells.cols
    g.rows = cells.rows
    const box = g.kind === 'move' ? boxMoved(g.start, cells.cols, cells.rows) : boxResized(g.start, g.handle!, cells.cols, cells.rows, g.type)
    const layout = pushed(g.base, g.id, box)
    if (layout) {
      placeTile(store.doc.value, layout)
      target.value = box
      badge.value = g.kind === 'move'
        ? t('dashboardDesigner.at', { col: box.x + 1, row: box.y + 1 })
        : t('dashboardDesigner.size', { w: box.w, h: box.h })
    }
  }
  // between the cells the tile follows the pointer, with nothing else to draw
  if (g.kind === 'move' && g.el && target.value) {
    const step = (g.width + GRID_GAP_PX) / GRID_COLUMNS
    g.el.style.transform = `translate3d(${dx - (target.value.x - g.start.x) * step}px, ${dy - (target.value.y - g.start.y) * STEP_Y}px, 0)`
  }
}

function edgeScroll() {
  edgeTimer = 0
  const el = stage.value
  const p = lastPointer
  if (!g?.moved || !el || !p) return
  const r = el.getBoundingClientRect()
  const EDGE = 32
  const speed = (over: number) => Math.round(Math.min(EDGE, Math.max(0, over)) / 2) + 2
  const vy = p.clientY > r.bottom - EDGE ? speed(p.clientY - (r.bottom - EDGE)) : p.clientY < r.top + EDGE ? -speed(r.top + EDGE - p.clientY) : 0
  if (!vy) return
  const before = el.scrollTop
  el.scrollBy(0, vy)
  if (el.scrollTop === before) return
  edgeTimer = requestAnimationFrame(edgeScroll)
  onMove(p)
}

function endGesture() {
  if (edgeTimer) { cancelAnimationFrame(edgeTimer); edgeTimer = 0 }
  lastPointer = null
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  window.removeEventListener('pointercancel', onUp)
}
function onUp() {
  endGesture()
  const done = g
  g = null
  if (done?.el) done.el.style.transform = ''
  moving.value = null
  target.value = null
  badge.value = ''
  store.gesture.value = false
  if (done?.moved) {
    const now = tiles.value.find((x) => x.id === done.id)
    if (now) emit('said', done.kind === 'move' ? t('dashboardDesigner.at', { col: now.grid.x + 1, row: now.grid.y + 1 }) : t('dashboardDesigner.size', { w: now.grid.w, h: now.grid.h }))
  }
}
onBeforeUnmount(endGesture)

function clearSelection() {
  store.select(null)
}

// ── drops from the panels ──
interface DropHint extends GridBox { on: string | null }
const dropHint = ref<DropHint | null>(null)
const payloadOf = (e: DragEvent) => (e.dataTransfer?.types.includes(DRAG_TYPE) ? dragged.value : null)
function cellAt(e: DragEvent, w: number, h: number): { x: number; y: number } {
  const r = board.value!.getBoundingClientRect()
  const step = (r.width + GRID_GAP_PX) / GRID_COLUMNS
  return {
    x: Math.max(0, Math.min(GRID_COLUMNS - w, Math.round((e.clientX - r.left) / step - w / 2))),
    y: Math.max(0, Math.round((e.clientY - r.top) / STEP_Y - h / 2)),
  }
}
function onDragOver(e: DragEvent) {
  const p = payloadOf(e)
  if (!p || !board.value) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  if (p.kind === 'field') {
    const r = board.value.getBoundingClientRect()
    const step = (r.width + GRID_GAP_PX) / GRID_COLUMNS
    const [cx, cy] = [Math.floor((e.clientX - r.left) / step), Math.floor((e.clientY - r.top) / STEP_Y)]
    const over = tiles.value.find((x) => cx >= x.grid.x && cx < x.grid.x + x.grid.w && cy >= x.grid.y && cy < x.grid.y + x.grid.h)
    if (over) { dropHint.value = { ...over.grid, on: over.id }; return }
    dropHint.value = { ...cellAt(e, 3, 3), w: 3, h: 3, on: null }
  } else if (p.kind === 'insert') {
    const size = sizeOfNew(p.type as TileKind)
    dropHint.value = { ...cellAt(e, size.w, size.h), ...size, on: null }
  }
}
function onDragLeave(e: DragEvent) {
  const to = e.relatedTarget as Node | null
  if (!to || !stage.value?.contains(to)) dropHint.value = null
}
function onDrop(e: DragEvent) {
  const p = payloadOf(e)
  const hint = dropHint.value
  dropHint.value = null
  dragged.value = null
  if (!p || !hint) return
  e.preventDefault()
  if (p.kind === 'insert') emit('insert', p.type as TileKind, { x: hint.x, y: hint.y })
  else if (p.kind === 'field') emit('drop-field', p.dataset, p.field, { x: hint.x, y: hint.y }, hint.on)
}

// the tile selected comes into view (a new one, one picked from a problem), and the keys go on with it
watch(() => store.selectedId.value, async (id) => {
  if (!id) return
  await nextTick()
  stage.value?.querySelector<HTMLElement>(`[data-tile="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  const active = document.activeElement
  if (!active || active === document.body || active.closest('.rds-tile, .rds-field')) stage.value?.focus({ preventScroll: true })
})
</script>

<template>
  <div
    ref="stage"
    class="ddc-stage"
    tabindex="0"
    role="group"
    :aria-label="$t('dashboardDesigner.canvasLabel')"
    @dragenter="onDragOver"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
    @pointerdown.self="clearSelection"
  >
    <div ref="board" class="ddc-board" :class="{ dragging: !!moving || !!dropHint }" :style="{ height: boardHeight }" @pointerdown.self="clearSelection">
      <div class="ddc-cells" aria-hidden="true" />

      <!-- where to start: the data, then a tile -->
      <div v-if="!tiles.length" class="ddc-start">
        <SquareDashedMousePointer :size="28" aria-hidden="true" />
        <template v-if="noData">
          <h2>{{ $t('dashboardDesigner.startTitle') }}</h2>
          <p>{{ $t('dashboardDesigner.startBody') }}</p>
          <button type="button" class="primary" data-start="dataset" @click="emit('add-dataset')"><Database :size="14" /> {{ $t('dashboardDesigner.startAction') }}</button>
        </template>
        <template v-else>
          <h2>{{ $t('dashboardDesigner.nextTitle') }}</h2>
          <p>{{ $t('dashboardDesigner.nextBody') }}</p>
          <div class="ddc-startrow">
            <button v-for="k in (['kpi', 'chart', 'table'] as const)" :key="k" type="button" :data-start="k" @click="emit('insert', k)"><Plus :size="14" /> {{ $t(`reportDesigner.items.${k}`) }}</button>
          </div>
        </template>
      </div>

      <!-- where it will land -->
      <div v-if="moving && target" class="ddc-target" :style="boxStyle(target)" aria-hidden="true" />
      <div v-if="dropHint && !dropHint.on" class="ddc-target" :style="boxStyle(dropHint)" aria-hidden="true" />

      <div
        v-for="tile in ordered"
        :key="tile.id"
        class="ddc-tile"
        :class="{ selected: store.selectedId.value === tile.id, bad: store.itemsWithProblems.value.has(tile.id), moving: moving === tile.id, over: dropHint?.on === tile.id }"
        :style="boxStyle(tile.grid)"
        :data-tile="tile.id"
        role="button"
        tabindex="0"
        :aria-pressed="store.selectedId.value === tile.id"
        :aria-label="$t('dashboardDesigner.tileLabel', { kind: kindName(tile), name: nameOf(tile), col: tile.grid.x + 1, row: tile.grid.y + 1, w: tile.grid.w, h: tile.grid.h })"
        @pointerdown="grab($event, tile)"
        @focus="store.selectedId.value !== tile.id && store.select(tile.id)"
      >
        <div class="ddc-face" inert>
          <div v-if="!ready(tile)" class="ddc-note ddc-need">
            <span class="ddc-kind">{{ kindName(tile) }}<template v-if="tile.title"> · {{ tile.title }}</template></span>
            <span>{{ needs(tile) }}</span>
          </div>
          <div v-else-if="hiddenNow(tile.id)" class="ddc-note">
            <EyeOff :size="15" aria-hidden="true" />
            <span class="ddc-kind">{{ nameOf(tile) }}</span>
            <span>{{ $t('dashboardDesigner.hiddenNow') }}</span>
          </div>
          <DashboardTile v-else :tile="tile" :entry="entryOf(tile.id)" :ctx="ctx" drawing />
        </div>
        <template v-if="store.selectedId.value === tile.id">
          <span v-for="h in HANDLES" :key="h" class="ddc-handle" :class="h" :data-handle="h" @pointerdown="begin($event, tile, 'resize', h)" />
          <span v-if="badge" class="ddc-badge">{{ badge }}</span>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ddc-stage { position: relative; overflow: auto; min-height: 0; height: 100%; outline: none; padding: 14px; box-sizing: border-box; background: var(--bg); }
.ddc-stage:focus-visible { box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--accent) 45%, transparent); }
/* the room at 1280 px, whose tiles stay readable; narrower it scrolls */
.ddc-board { position: relative; min-width: 700px; }
/* twelve columns and their rows: only so much that the eye finds a cell */
.ddc-cells {
  position: absolute; inset: 0; pointer-events: none; opacity: 0.35; transition: opacity 0.12s ease; border-radius: 6px;
  background-image:
    linear-gradient(to bottom, transparent 40px, var(--bg) 40px),
    linear-gradient(to right, var(--panel-2) calc(100% - 10px), transparent calc(100% - 10px));
  background-size: 100% 50px, calc((100% + 10px) / 12) 100%;
}
.ddc-board.dragging .ddc-cells { opacity: 1; }
.ddc-tile {
  position: absolute; box-sizing: border-box; cursor: grab; border-radius: 10px; outline: none; touch-action: none; user-select: none;
  transition: left 0.12s ease, top 0.12s ease;
}
.ddc-tile:focus-visible { box-shadow: 0 0 0 2px var(--bg), 0 0 0 4px color-mix(in srgb, var(--accent) 60%, transparent); }
.ddc-tile.selected { box-shadow: 0 0 0 2px var(--accent); z-index: 3; }
.ddc-tile.over { box-shadow: 0 0 0 2px var(--accent-2); }
.ddc-tile.bad { box-shadow: 0 0 0 2px var(--danger); }
.ddc-tile.moving { cursor: grabbing; z-index: 5; opacity: 0.92; transition: none; box-shadow: 0 0 0 2px var(--accent), var(--shadow-2); }
.ddc-face { position: absolute; inset: 0; pointer-events: none; display: flex; border-radius: inherit; overflow: hidden; }
.ddc-face > :deep(.tile) { flex: 1; min-width: 0; }
.ddc-face :deep(.failed .mini) { display: none; } /* the next change asks again; here it cannot be reached */
.ddc-note {
  flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; padding: 8px 10px; text-align: center;
  font-size: 12px; line-height: 1.35; color: var(--muted); background: var(--panel); border: 1px dashed var(--border); border-radius: inherit; overflow: hidden;
}
.ddc-need { background: color-mix(in srgb, var(--panel) 70%, transparent); }
.ddc-kind { font-weight: 600; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.ddc-target { position: absolute; pointer-events: none; border: 1.5px dashed var(--accent); border-radius: 10px; background: color-mix(in srgb, var(--accent) 10%, transparent); z-index: 1; }
.ddc-handle {
  position: absolute; z-index: 7; width: 9px; height: 9px; margin: -4.5px 0 0 -4.5px; border-radius: 2px;
  background: var(--panel); border: 1.5px solid var(--accent); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
}
/* a larger target than the eye sees: a handle is small, and the pointer is not */
.ddc-handle::after { content: ""; position: absolute; inset: -5px; }
.ddc-handle.nw { left: 0; top: 0; cursor: nwse-resize; }
.ddc-handle.n { left: 50%; top: 0; cursor: ns-resize; }
.ddc-handle.ne { left: 100%; top: 0; cursor: nesw-resize; }
.ddc-handle.e { left: 100%; top: 50%; cursor: ew-resize; }
.ddc-handle.se { left: 100%; top: 100%; cursor: nwse-resize; }
.ddc-handle.s { left: 50%; top: 100%; cursor: ns-resize; }
.ddc-handle.sw { left: 0; top: 100%; cursor: nesw-resize; }
.ddc-handle.w { left: 0; top: 50%; cursor: ew-resize; }
.ddc-badge {
  position: absolute; left: 0; top: calc(100% + 6px); z-index: 8; pointer-events: none; white-space: nowrap;
  font-size: 11px; line-height: 1; font-variant-numeric: tabular-nums; color: var(--on-accent); background: var(--accent); padding: 4px 7px; border-radius: 6px; box-shadow: var(--shadow-1);
}
.ddc-start {
  position: relative; z-index: 1; max-width: 460px; margin: 10vh auto 0; padding: 26px 24px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center;
  color: var(--muted); background: var(--panel); border: 1px solid var(--border-soft); border-radius: 12px;
}
.ddc-start > svg { color: var(--accent); }
.ddc-start h2 { margin: 4px 0 0; font-size: 15px; font-weight: 600; color: var(--text); }
.ddc-start p { margin: 0 0 6px; font-size: 13px; line-height: 1.5; }
.ddc-start button { display: inline-flex; align-items: center; gap: 6px; }
.ddc-startrow { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }
@media (prefers-reduced-motion: reduce) {
  .ddc-tile, .ddc-cells { transition: none; }
}
</style>
