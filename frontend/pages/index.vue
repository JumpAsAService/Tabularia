<script setup lang="ts">
// The home page (docs/specs/home.md): a grid of widgets each person arranges for themselves, kept
// by the gateway (GET/PUT /home). Reading by default; «Customise» turns on moving, resizing,
// adding and removing, by pointer and by keyboard, each change saved as it is made, with undo.
//
// The grid is the dashboards' (composables/dashboardGrid.ts): 12 columns on a wide window, 6 on a
// medium one, one on a phone, in reading order, which is also the order for the keyboard and for
// whoever listens to the page. Arranging needs the 12 columns, so it is offered on a wide window.
// A widget shows only what its reader may see elsewhere: each asks the page's own endpoint.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, type Component } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import {
  AlarmClock, CalendarClock, ChartColumn, ChartNoAxesColumn, Check, Clock3, Cpu, Database, DatabaseBackup, GripVertical, History, LoaderCircle,
  Megaphone, Plus, Redo2, RotateCcw, Settings2, Star, StickyNote, Undo2, Users, X, XCircle, Zap,
} from 'lucide-vue-next'
import HomeBackups from '~/components/home/HomeBackups.vue'
import HomeClock from '~/components/home/HomeClock.vue'
import HomeDatasources from '~/components/home/HomeDatasources.vue'
import HomeFailedRuns from '~/components/home/HomeFailedRuns.vue'
import HomeFavourites from '~/components/home/HomeFavourites.vue'
import HomeFlowRuns from '~/components/home/HomeFlowRuns.vue'
import HomeNextRuns from '~/components/home/HomeNextRuns.vue'
import HomeNote from '~/components/home/HomeNote.vue'
import HomeNotices from '~/components/home/HomeNotices.vue'
import HomeOptions from '~/components/home/HomeOptions.vue'
import HomePulse from '~/components/home/HomePulse.vue'
import HomeQueue from '~/components/home/HomeQueue.vue'
import HomeRecent from '~/components/home/HomeRecent.vue'
import HomeRunningNow from '~/components/home/HomeRunningNow.vue'
import HomeRunsByDay from '~/components/home/HomeRunsByDay.vue'
import HomeSessions from '~/components/home/HomeSessions.vue'
import HomeShortcuts from '~/components/home/HomeShortcuts.vue'
import { GRID_GAP_PX, GRID_ROW_PX, MEDIUM_FROM_PX, WIDE_FROM_PX, cellsOf, columnsOf, gridWidth, placeTiles, type GridBox, type GridWidth } from '~/composables/dashboardGrid'
import {
  HOME_TYPES, added, bodyOf, greetingKey, history, knownWidgets, moved, removed, resized, tilesOf, withOptions,
  type HomeAnswer, type HomeWidget,
} from '~/composables/homeLayout'

const { t } = useI18n()
const { apiFetch } = useApiClient()
const toast = useToast()
const fmt = useFormat()
useHead({ title: () => `${t('nav.home')} · Tabularia` })

const FACES: Record<string, Component> = {
  clock: HomeClock, shortcuts: HomeShortcuts, favourites: HomeFavourites, recent: HomeRecent, flow_runs: HomeFlowRuns, runs_by_day: HomeRunsByDay,
  running_now: HomeRunningNow, failed_runs: HomeFailedRuns, next_runs: HomeNextRuns, datasources: HomeDatasources, notices: HomeNotices,
  note: HomeNote, queue: HomeQueue, sessions: HomeSessions, backups: HomeBackups,
}
// each widget's icon, and the tone it wears: the product's own category colours (assets/main.css),
// by what the widget is about — runs, what needs care, time, the person's own, the installation
const ICONS: Record<string, Component> = {
  clock: Clock3, shortcuts: Zap, favourites: Star, recent: History, flow_runs: ChartNoAxesColumn, runs_by_day: ChartColumn,
  running_now: LoaderCircle, failed_runs: XCircle, next_runs: CalendarClock, datasources: Database, notices: Megaphone,
  note: StickyNote, queue: Cpu, sessions: Users, backups: DatabaseBackup,
}
const TONES: Record<string, string> = {
  clock: 'time', next_runs: 'time', shortcuts: 'own', favourites: 'own', recent: 'own', note: 'own', flow_runs: 'runs', runs_by_day: 'runs',
  running_now: 'runs', datasources: 'runs', failed_runs: 'care', notices: 'care', queue: 'admin', sessions: 'admin', backups: 'admin',
}
// widgets that have nothing to set: no gear
const NO_OPTIONS = new Set(['favourites', 'running_now', 'notices', 'queue', 'sessions', 'backups'])
const { user } = useAuth()
const greeting = computed(() => {
  const first = (user.value?.full_name || '').trim().split(/\s+/)[0]
  const key = `home.greeting.${greetingKey(new Date().getHours())}`
  return first ? t(`${key}Name`, { name: first }) : t(key)
})
const today = computed(() => fmt.date(new Date(), { weekday: 'long', day: 'numeric', month: 'long' }))

const widgets = ref<HomeWidget[]>([])
/** widgets of a type this interface cannot draw (a newer gateway's): kept as they are, and saved back */
let beyond: HomeWidget[] = []
const available = ref<string[]>([])
const isDefault = ref(true)
const state = ref<'loading' | 'ready' | 'failed'>('loading')

function take(a: HomeAnswer) {
  widgets.value = knownWidgets(a.widgets)
  beyond = a.widgets.filter((w) => !widgets.value.includes(w))
  available.value = a.available.filter((x) => (HOME_TYPES as readonly string[]).includes(x))
  isDefault.value = a.is_default
}
async function load() {
  state.value = 'loading'
  try {
    take(await apiFetch<HomeAnswer>('/home'))
    state.value = 'ready'
  } catch {
    state.value = 'failed'
  }
}

// ── the width: three layouts, and arranging on the widest ──
const width = ref<GridWidth>('wide')
let queries: MediaQueryList[] = []
const measure = () => {
  width.value = gridWidth(window.innerWidth)
  if (width.value !== 'wide' && editing.value) done()
}
const placed = computed(() => placeTiles(tilesOf(widgets.value), width.value))
const byId = computed(() => new Map(widgets.value.map((w) => [w.id, w])))
const gridStyle = computed(() => ({
  gridTemplateColumns: `repeat(${columnsOf(width.value)}, minmax(0, 1fr))`,
  // on a phone a widget is as tall as what it holds: a height drawn for twelve columns would cut it
  gridAutoRows: width.value === 'narrow' ? 'auto' : `${GRID_ROW_PX}px`,
  gap: `${GRID_GAP_PX}px`,
}))

// ── arranging ──
const editing = ref(false)
const selected = ref<string | null>(null)
const optionsOf = ref<HomeWidget | null>(null)
const said = ref('') // what a screen reader is told of a change
const saving = ref<'idle' | 'saving' | 'saved' | 'failed'>('idle')
const hist = history<HomeWidget[]>()
const canUndo = ref(false)
const canRedo = ref(false)
const board = ref<HTMLElement | null>(null)
const addBtn = ref<HTMLElement | null>(null)
const catalogue = ref(false)
const nameOf = (w: HomeWidget) => t(`home.widgets.${w.type}`)
const refreshHistory = () => { canUndo.value = hist.canUndo(); canRedo.value = hist.canRedo() }

let saveTimer: ReturnType<typeof setTimeout> | undefined
let unsaved = false // a change waits for its save
let saveAgain = false
let inFlight = false
function scheduleSave() {
  unsaved = true
  clearTimeout(saveTimer)
  saveTimer = setTimeout(save, 500)
}
async function save() {
  clearTimeout(saveTimer)
  unsaved = false
  if (inFlight) { saveAgain = true; return }
  inFlight = true
  saving.value = 'saving'
  try {
    await apiFetch('/home', { method: 'PUT', body: bodyOf([...widgets.value, ...beyond]) })
    isDefault.value = false
    saving.value = 'saved'
  } catch (e) {
    saving.value = 'failed'
    toast.error(t('home.saveFailed'))
  } finally {
    inFlight = false
    if (saveAgain) { saveAgain = false; save() }
  }
}
/** A change of the layout: one step of the history, shown at once, saved a moment later. */
function change(next: HomeWidget[] | null, announce = '', from?: HomeWidget[]) {
  if (!next) return false
  hist.record(from ?? widgets.value)
  widgets.value = next
  refreshHistory()
  if (announce) said.value = announce
  scheduleSave()
  return true
}
function undo() {
  const p = hist.undo(widgets.value)
  if (!p) return
  widgets.value = p; refreshHistory(); said.value = t('home.undone'); scheduleSave()
}
function redo() {
  const f = hist.redo(widgets.value)
  if (!f) return
  widgets.value = f; refreshHistory(); said.value = t('home.redone'); scheduleSave()
}
function customise() {
  editing.value = true
  said.value = t('home.editingOn')
}
function done() {
  editing.value = false
  selected.value = null
  catalogue.value = false
  if (unsaved) save()
}
async function add(type: string) {
  catalogue.value = false
  const r = added(widgets.value, type)
  if (!r) { toast.error(t('home.tooMany')); return }
  change(r.widgets, t('home.added', { name: t(`home.widgets.${type}`) }))
  selected.value = r.id
  await nextTick()
  const el = board.value?.querySelector<HTMLElement>(`[data-widget="${CSS.escape(r.id)}"]`)
  el?.scrollIntoView({ block: 'nearest' })
  el?.focus()
}
async function remove(w: HomeWidget) {
  const i = placed.value.findIndex((p) => p.id === w.id)
  change(removed(widgets.value, w.id), t('home.removed', { name: nameOf(w) }))
  selected.value = null
  // the focus goes to the widget that follows, or to «+ Widget» when none is left
  await nextTick()
  const next = placed.value[Math.min(i, placed.value.length - 1)]
  const el = next ? board.value?.querySelector<HTMLElement>(`[data-widget="${CSS.escape(next.id)}"]`) : addBtn.value
  el?.focus()
}
function saveOptions(options: Record<string, any>) {
  const w = optionsOf.value
  optionsOf.value = null
  if (w) change(withOptions(widgets.value, w.id, options), t('home.optionsSaved', { name: nameOf(w) }))
}
async function reset() {
  if (!confirm(t('home.resetConfirm'))) return
  try {
    clearTimeout(saveTimer)
    unsaved = false // what waited to be saved is what the reset throws away
    take(await apiFetch<HomeAnswer>('/home/reset', { method: 'POST' }))
    hist.clear(); refreshHistory()
    saving.value = 'idle'
    said.value = t('home.resetDone')
  } catch {
    toast.error(t('home.saveFailed'))
  }
}

// ── the keyboard, on a widget being arranged ──
const where = (w: HomeWidget) => t('home.at', { name: nameOf(w), col: w.x + 1, row: w.y + 1, w: w.w, h: w.h })
function onWidgetKey(e: KeyboardEvent, w: HomeWidget) {
  if (!editing.value || e.target !== e.currentTarget || e.ctrlKey || e.metaKey || e.altKey) return
  const arrows: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }
  const dir = arrows[e.key]
  if (dir) {
    e.preventDefault()
    const next = e.shiftKey ? resized(widgets.value, w.id, 'se', dir[0], dir[1]) : moved(widgets.value, w.id, dir[0], dir[1])
    const now = next?.find((x) => x.id === w.id)
    if (now && (now.x !== w.x || now.y !== w.y || now.w !== w.w || now.h !== w.h)) change(next, where(now))
    else said.value = t('home.edge', { name: nameOf(w) })
    // the widget keeps the focus where it lands
    nextTick(() => board.value?.querySelector<HTMLElement>(`[data-widget="${CSS.escape(w.id)}"]`)?.focus())
  } else if (e.key === 'Delete' || e.key === 'Backspace') {
    e.preventDefault()
    remove(w)
  } else if (e.key === 'Enter' && !NO_OPTIONS.has(w.type)) {
    e.preventDefault()
    optionsOf.value = w
  }
}
function onPageKey(e: KeyboardEvent) {
  if (!editing.value || optionsOf.value) return
  const el = e.target as HTMLElement
  if (el.closest('input, textarea, select, [contenteditable]')) return
  const mod = e.ctrlKey || e.metaKey
  if (mod && !e.altKey && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo() }
  else if (mod && !e.altKey && e.key.toLowerCase() === 'y') { e.preventDefault(); redo() }
  else if (e.key === 'Escape' && catalogue.value) { catalogue.value = false; addBtn.value?.focus() }
}

// ── the pointer: a press on the head moves, the corner resizes; one gesture is one step ──
interface Gesture { kind: 'move' | 'resize'; id: string; start: GridBox; base: HomeWidget[]; sx: number; sy: number; width: number; cols: number; rows: number; changed: boolean }
let g: Gesture | null = null
const dragging = ref<string | null>(null)
function begin(e: PointerEvent, w: HomeWidget, kind: 'move' | 'resize') {
  if (!editing.value || e.button !== 0 || !board.value) return
  if (kind === 'move' && (e.target as HTMLElement).closest('button')) return
  e.preventDefault()
  selected.value = w.id
  g = { kind, id: w.id, start: { x: w.x, y: w.y, w: w.w, h: w.h }, base: widgets.value, sx: e.clientX, sy: e.clientY, width: board.value.clientWidth, cols: 0, rows: 0, changed: false }
  dragging.value = w.id
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  window.addEventListener('pointercancel', onUp)
}
function onMove(e: PointerEvent) {
  if (!g) return
  const cells = cellsOf(e.clientX - g.sx, e.clientY - g.sy, g.width)
  if (cells.cols === g.cols && cells.rows === g.rows) return
  g.cols = cells.cols
  g.rows = cells.rows
  // always from the layout the gesture started with: what was pushed goes back when the widget moves on
  const next = g.kind === 'move' ? moved(g.base, g.id, cells.cols, cells.rows, g.start) : resized(g.base, g.id, 'se', cells.cols, cells.rows, g.start)
  if (next) { widgets.value = next; g.changed = true }
}
function onUp() {
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  window.removeEventListener('pointercancel', onUp)
  const was = g
  g = null
  dragging.value = null
  if (!was?.changed) return
  const now = widgets.value.find((x) => x.id === was.id)
  const next = widgets.value
  widgets.value = was.base
  change(next, now ? where(now) : '', was.base)
}

onMounted(() => {
  queries = [WIDE_FROM_PX, MEDIUM_FROM_PX].map((px) => window.matchMedia(`(min-width: ${px}px)`))
  queries.forEach((q) => q.addEventListener('change', measure))
  measure()
  load()
})
onBeforeUnmount(() => {
  queries.forEach((q) => q.removeEventListener('change', measure))
  onUp()
  if (unsaved) save() // a change made a moment before leaving is not lost
})
</script>

<template>
  <AppShell>
    <BannerBar />
    <div class="home" @keydown="onPageKey">
      <header class="home-head">
        <div class="hello">
          <h1>{{ greeting }}</h1>
          <p class="today">{{ today }}</p>
        </div>
        <span class="status" role="status">
          <template v-if="editing">{{ saving === 'saving' ? $t('home.saving') : saving === 'saved' ? $t('home.saved') : saving === 'failed' ? $t('home.saveFailed') : '' }}</template>
        </span>
        <template v-if="state === 'ready'">
          <template v-if="editing">
            <div class="addwrap">
              <button ref="addBtn" type="button" :aria-expanded="catalogue" aria-controls="home-catalogue" @click="catalogue = !catalogue">
                <Plus :size="14" aria-hidden="true" /> {{ $t('home.add') }}
              </button>
              <div v-if="catalogue" id="home-catalogue" class="menu">
                <button v-for="type in available" :key="type" type="button" class="menu-item" :data-tone="TONES[type]" @click="add(type)">
                  <span class="chip" aria-hidden="true"><component :is="ICONS[type]" :size="14" /></span>
                  <span class="mi-text">
                    <span class="mi-name">{{ $t(`home.widgets.${type}`) }}</span>
                    <span class="mi-what">{{ $t(`home.about.${type}`) }}</span>
                  </span>
                </button>
              </div>
            </div>
            <button type="button" :disabled="!canUndo" :title="$t('home.undo')" :aria-label="$t('home.undo')" @click="undo"><Undo2 :size="14" aria-hidden="true" /></button>
            <button type="button" :disabled="!canRedo" :title="$t('home.redo')" :aria-label="$t('home.redo')" @click="redo"><Redo2 :size="14" aria-hidden="true" /></button>
            <button type="button" :disabled="isDefault" @click="reset"><RotateCcw :size="14" aria-hidden="true" /> {{ $t('home.reset') }}</button>
            <button type="button" class="primary" @click="done"><Check :size="14" aria-hidden="true" /> {{ $t('home.done') }}</button>
          </template>
          <button v-else-if="width === 'wide'" type="button" class="custom" @click="customise"><Settings2 :size="14" aria-hidden="true" /> {{ $t('home.customise') }}</button>
        </template>
      </header>
      <HomePulse v-if="state === 'ready' && !editing" />
      <p v-if="editing" class="help">{{ $t('home.editHelp') }}</p>
      <p class="sr-only" aria-live="polite">{{ said }}</p>

      <p v-if="state === 'loading'" class="msg" aria-busy="true"><span class="sk" style="width: 240px" /></p>
      <p v-else-if="state === 'failed'" class="msg" role="alert">
        {{ $t('home.loadFailed') }} <button type="button" @click="load">{{ $t('home.retry') }}</button>
      </p>
      <div v-else-if="!widgets.length" class="empty">
        <p>{{ $t('home.empty') }}</p>
        <button v-if="!editing && width === 'wide'" type="button" class="primary" @click="customise">{{ $t('home.customise') }}</button>
      </div>

      <div v-else ref="board" class="board" :class="{ editing }" :style="gridStyle">
        <section
          v-for="p in placed"
          :key="p.id"
          class="hw"
          :class="{ selected: editing && selected === p.id, dragging: dragging === p.id }"
          :data-widget="p.id"
          :data-type="byId.get(p.id)!.type"
          :data-tone="TONES[byId.get(p.id)!.type]"
          :aria-labelledby="`hw-${p.id}`"
          :aria-describedby="editing ? 'home-keys' : undefined"
          :tabindex="editing ? 0 : undefined"
          :style="width === 'narrow' ? undefined : { gridColumn: `${p.x + 1} / span ${p.w}`, gridRow: `${p.y + 1} / span ${p.h}` }"
          @focus="selected = p.id"
          @keydown="onWidgetKey($event, byId.get(p.id)!)"
        >
          <header class="hw-head" @pointerdown="begin($event, byId.get(p.id)!, 'move')">
            <GripVertical v-if="editing" :size="14" class="grip" aria-hidden="true" />
            <span class="chip" aria-hidden="true"><component :is="ICONS[byId.get(p.id)!.type]" :size="13" /></span>
            <h2 :id="`hw-${p.id}`">{{ nameOf(byId.get(p.id)!) }}</h2>
            <template v-if="editing">
              <button v-if="!NO_OPTIONS.has(byId.get(p.id)!.type)" type="button" class="tool" :title="$t('home.options')" :aria-label="$t('home.optionsOf', { name: nameOf(byId.get(p.id)!) })" @click="optionsOf = byId.get(p.id)!"><Settings2 :size="14" aria-hidden="true" /></button>
              <button type="button" class="tool" :title="$t('home.remove')" :aria-label="$t('home.removeOf', { name: nameOf(byId.get(p.id)!) })" @click="remove(byId.get(p.id)!)"><X :size="14" aria-hidden="true" /></button>
            </template>
          </header>
          <!-- while arranging, the widget's own links are out of reach: a press moves it -->
          <div class="hw-body" :inert="editing">
            <component :is="FACES[byId.get(p.id)!.type]" :options="byId.get(p.id)!.options" />
          </div>
          <span v-if="editing" class="hw-resize" aria-hidden="true" @pointerdown="begin($event, byId.get(p.id)!, 'resize')" />
        </section>
      </div>
      <p v-if="editing" id="home-keys" class="sr-only">{{ $t('home.keys') }}</p>
    </div>
    <HomeOptions :widget="optionsOf" @save="saveOptions" @close="optionsOf = null" />
  </AppShell>
</template>

<style scoped>
.home { flex: 1; min-width: 0; padding: 22px 24px 32px; }
.home-head { display: flex; align-items: flex-end; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.hello { min-width: 0; }
.hello h1 { margin: 0; font-size: 24px; font-weight: 700; line-height: 1.15; letter-spacing: -0.015em; text-wrap: balance; }
.today { margin: 4px 0 0; color: var(--muted); font-size: 13px; }
.today::first-letter { text-transform: uppercase; }
.status { flex: 1; min-width: 0; color: var(--muted); font-size: 12.5px; text-align: right; }
.custom { color: var(--muted); }
.help { margin: -6px 0 14px; color: var(--muted); font-size: 12.5px; }
.msg { color: var(--muted); font-size: 13px; }
.msg .sk { display: inline-block; height: 14px; }
.empty { display: grid; place-items: center; gap: 12px; padding: 60px 0; color: var(--muted); font-size: 14px; }
.empty p { margin: 0; }

/* the tone of a widget: one of the product's category colours, on its icon alone */
[data-tone="runs"] { --tone: var(--accent-2); }
[data-tone="care"] { --tone: var(--danger); }
[data-tone="time"] { --tone: var(--violet); }
[data-tone="own"] { --tone: var(--accent); }
[data-tone="admin"] { --tone: var(--warning); }
.chip {
  display: grid; place-items: center; width: 22px; height: 22px; border-radius: 6px; flex-shrink: 0;
  color: var(--tone, var(--accent)); background: color-mix(in srgb, var(--tone, var(--accent)) 14%, transparent);
}

.addwrap { position: relative; }
.menu {
  position: absolute; right: 0; top: calc(100% + 6px); z-index: 50; width: 340px; max-height: min(70vh, 560px); overflow: auto; padding: 6px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px; box-shadow: var(--shadow-2);
}
.menu-item { display: flex; align-items: flex-start; gap: 10px; width: 100%; padding: 8px 10px; border: none; border-radius: 8px; background: none; text-align: left; }
.menu-item:hover, .menu-item:focus-visible { background: var(--tint-accent); box-shadow: none; }
.mi-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.mi-name { font-size: 13px; font-weight: 600; color: var(--text); }
.mi-what { font-size: 12px; color: var(--muted); white-space: normal; }

.board { display: grid; min-width: 0; }
.hw {
  display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; container-type: inline-size;
  background: var(--panel); border: 1px solid var(--border-soft); border-radius: 14px;
}
.hw-head { display: flex; align-items: center; gap: 8px; padding: 12px 14px 6px; min-height: 40px; }
.hw-head h2 { flex: 1; min-width: 0; margin: 0; font-size: 13px; font-weight: 650; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hw-body { flex: 1; min-height: 0; padding: 4px 14px 14px; }
.board:not(.editing) .hw:hover { border-color: var(--border); }
.board.editing .hw { border-style: dashed; border-color: var(--border); position: relative; }
.board.editing .hw-head { cursor: grab; touch-action: none; user-select: none; }
.board.editing .hw.dragging { box-shadow: var(--shadow-pin, var(--shadow-2)); border-color: var(--accent); z-index: 2; }
.board.editing .hw.dragging .hw-head { cursor: grabbing; }
.board.editing .hw.selected, .board.editing .hw:focus-visible { border-style: solid; border-color: var(--accent); outline: 2px solid var(--accent); outline-offset: 1px; }
.board.editing .hw-body { opacity: 0.7; }
.grip { color: var(--muted); flex-shrink: 0; margin-left: -4px; }
.tool { display: grid; place-items: center; width: 26px; height: 26px; min-height: 0; padding: 0; border: none; border-radius: 7px; background: none; color: var(--muted); }
.tool:hover, .tool:focus-visible { background: var(--panel-2); color: var(--text); box-shadow: none; }
.hw-resize {
  position: absolute; right: 0; bottom: 0; width: 18px; height: 18px; cursor: nwse-resize; touch-action: none;
  background: linear-gradient(135deg, transparent 50%, var(--muted) 50%, var(--muted) 60%, transparent 60%, transparent 72%, var(--muted) 72%, var(--muted) 82%, transparent 82%);
  border-bottom-right-radius: 13px;
}
@media (prefers-reduced-motion: no-preference) {
  .hw { transition: border-color var(--dur-hover, 120ms) ease; }
}
@media (max-width: 699px) {
  .hw-body { max-height: 440px; overflow: auto; }
  .hw[data-type="runs_by_day"] .hw-body, .hw[data-type="clock"] .hw-body { height: 220px; }
  .hw[data-type="clock"] .hw-body { height: 110px; }
  .home { padding: 16px 14px 24px; }
  .hello h1 { font-size: 20px; }
}
</style>
