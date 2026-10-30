<script setup lang="ts">
// The page being designed, in millimetres at the chosen zoom: rulers (0 at the margins;
// the vertical one starts again in every band), the margins' frame, the three bands with
// their labels and the header's and footer's heights to drag, the items, and the
// gestures — moving, resizing with the handles, snapping to the millimetre and to the
// edges and centres of the other items, dropping elements and fields from the panels.
// The rules of the definition are shown while one works: the header and the footer refuse
// what they cannot hold, a table's row is its own (what is dropped beside it goes under
// it), nothing leaves the page.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import RdItem from '~/components/report/designer/RdItem.vue'
import RdChartGlyph from '~/components/report/designer/RdChartGlyph.vue'
import { BANDS, DRAG_TYPE, dragged, useDesign, type DragPayload } from '~/composables/reportDesignStore'
import {
  CHART_KINDS, allowedIn, bodyPageHeight, boxOf, columnFor, contentWidth, fieldRef, mm1, newItem, pageSize, rowConflicts,
  type BandName, type ChartKind, type DItem, type DText, type InsertKind, type ItemType,
} from '~/composables/reportDesignModel'

const store = useDesign()
const { t } = useI18n()
const toast = useToast()

const RULER = 22
const PAD_Y = 28

const def = computed(() => store.doc.value)
const ppm = computed(() => (store.zoom.value * 96) / 25.4)
const page = computed(() => pageSize(def.value))
const margins = computed(() => def.value.page.margins)
const W = computed(() => contentWidth(def.value))
const bodyPage = computed(() => bodyPageHeight(def.value))
const bodyDesign = computed(() => {
  const bottom = def.value.sections.body.items.reduce((m, i) => Math.max(m, boxOf(i).y + boxOf(i).h), 0)
  return Math.max(bodyPage.value, Math.ceil(bottom + 8))
})
const bandTop = computed<Record<BandName, number>>(() => {
  const top = margins.value[0]
  const hh = def.value.sections.page_header.height
  return { page_header: top, body: top + hh, page_footer: top + hh + bodyDesign.value }
})
const bandHeight = computed<Record<BandName, number>>(() => ({
  page_header: def.value.sections.page_header.height,
  body: bodyDesign.value,
  page_footer: def.value.sections.page_footer.height,
}))
const sheetH = computed(() => bandTop.value.page_footer + bandHeight.value.page_footer + margins.value[2])

// ── the desk's margins, as wide as what is written in them in this language: the bands'
// names on the left (they wrap at their spaces), a selected table's rows on the right ──
const NAME_FONT = "600 10.5px 'IBM Plex Sans', system-ui, sans-serif"
const NAME_SPACING = 0.525 // 0.05em
const SIZE_FONT = "11px 'IBM Plex Sans', system-ui, sans-serif"
const TAG_FONT = "600 9.5px 'IBM Plex Sans', system-ui, sans-serif"
const TAG_SPACING = 0.285 // 0.03em
const LABEL_PAD = 18 // a band label's padding, left and right
const LABEL_GAP = 10 // between a band label and the sheet
const NAME_LINE = 13
const SIZE_LINE = 14
let measurer: CanvasRenderingContext2D | null = null
const fontsReady = ref(0)
function textWidth(text: string, font: string, spacing = 0): number {
  void fontsReady.value // measured again once the font has arrived
  if (!measurer && typeof document !== 'undefined') measurer = document.createElement('canvas').getContext('2d')
  if (!measurer) return text.length * 7
  measurer.font = font
  return measurer.measureText(text).width + spacing * text.length
}
/** How many lines a text takes in `width`, cut at its spaces (a word wider than the
 *  width is broken by the browser, `overflow-wrap: anywhere`). */
function linesOf(text: string, font: string, width: number, spacing = 0): number {
  let lines = 1
  let line = 0
  const space = textWidth(' ', font, spacing)
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const w = textWidth(word, font, spacing)
    if (line && line + space + w > width) { lines++; line = w } else line += (line ? space : 0) + w
    if (w > width) lines += Math.floor(w / width)
  }
  return lines
}
const names = computed(() => Object.fromEntries(BANDS.map((b) => [b, t(`reportDesigner.bands.${b}`).toUpperCase()])) as Record<BandName, string>)
const sizeText = (b: BandName) => (b === 'body' ? t('reportDesigner.canvas.grows') : `${fmt(bandHeight.value[b])} mm`)
/** the width of a band label's text: its whole name on one line when that is not too wide,
 *  else its longest word; the body's «grows with its data» wraps */
const labelText = computed(() => {
  const all = Object.values(names.value)
  let need = Math.max(...all.map((n) => textWidth(n, NAME_FONT, NAME_SPACING)))
  if (need > 112) need = Math.max(...all.flatMap((n) => n.split(/\s+/)).map((w) => textWidth(w, NAME_FONT, NAME_SPACING)))
  const grows = t('reportDesigner.canvas.grows').split(/\s+/).map((w) => textWidth(w, SIZE_FONT))
  return Math.ceil(Math.min(140, Math.max(64, need, ...grows, textWidth('888.8 mm', SIZE_FONT))))
})
const gutterL = computed(() => labelText.value + LABEL_PAD + LABEL_GAP + 6)
// the rows' tags: the widest of them in this language, 12 px from the sheet
const gutterR = computed(() => {
  const tags = [t('reportDesigner.face.tagHead'), t('reportDesigner.face.tagGroupHead', { n: 6 }), t('reportDesigner.face.tagDetail'),
    t('reportDesigner.face.tagCells'), t('reportDesigner.face.tagGroupFoot', { n: 6 }), t('reportDesigner.face.tagTotal')]
  return Math.ceil(Math.max(...tags.map((x) => textWidth(x.toUpperCase(), TAG_FONT, TAG_SPACING)))) + 14 + 12 + 12
})
/** Where each band's label goes: at its band's top, or under the label above it when the
 *  bands are too thin at this zoom for both (nothing is written over anything). */
const labelTops = computed(() => {
  const out = {} as Record<BandName, number>
  let floor = -Infinity
  for (const b of BANDS) {
    const top = Math.max(yOf(b, 0), floor)
    const height = linesOf(names.value[b], NAME_FONT, labelText.value, NAME_SPACING) * NAME_LINE
      + linesOf(sizeText(b), SIZE_FONT, labelText.value) * SIZE_LINE + 10
    out[b] = top
    floor = top + height + 4
  }
  return out
})

// ── the stage: the sheet centred, the rulers stuck to the edges ──
const stage = ref<HTMLElement | null>(null)
const sheet = ref<HTMLElement | null>(null)
const stageW = ref(1000)
let observer: ResizeObserver | null = null
onMounted(() => {
  if (!stage.value) return
  // read after the frame: the sheet's new width can bring or take a scrollbar, which would
  // resize the stage again inside the same notification (the browser's «ResizeObserver loop»)
  observer = new ResizeObserver(() => requestAnimationFrame(() => {
    const w = stage.value?.clientWidth
    if (w && w !== stageW.value) stageW.value = w
  }))
  observer.observe(stage.value)
  stageW.value = stage.value.clientWidth
  // a page wider than the stage at 100%: it opens fitted to the width
  if (stageW.value - RULER < page.value.w * (96 / 25.4) * store.zoom.value + gutterL.value + gutterR.value) fitWidth()
  document.fonts?.ready.then(() => { fontsReady.value++ })
})
onBeforeUnmount(() => observer?.disconnect())
// a page made wider than the stage is fitted, as when it opens; made narrower again (an undo),
// it goes back to the zoom it had
let zoomBeforeFit: number | null = null
watch(() => page.value.w, (w, before) => {
  if (w > before && stageW.value - RULER < w * (96 / 25.4) * store.zoom.value + gutterL.value + gutterR.value) {
    zoomBeforeFit = store.zoom.value
    fitWidth()
  } else if (w < before && zoomBeforeFit != null) {
    store.zoom.value = zoomBeforeFit
    zoomBeforeFit = null
  }
})

const sheetW = computed(() => page.value.w * ppm.value)
const areaW = computed(() => Math.max(stageW.value - RULER - 2, sheetW.value + gutterL.value + gutterR.value))
const sheetLeft = computed(() => gutterL.value + Math.max(0, (areaW.value - gutterL.value - gutterR.value - sheetW.value) / 2))
const areaH = computed(() => sheetH.value * ppm.value + PAD_Y * 2)
/** where the content box starts on the stage's area, in px */
const originX = computed(() => sheetLeft.value + margins.value[3] * ppm.value)
const yOf = (band: BandName, mm: number) => PAD_Y + (bandTop.value[band] + mm) * ppm.value

/** The zoom that fits the page's width in the stage. */
function fitWidth() {
  const room = (stage.value?.clientWidth ?? 1000) - RULER - gutterL.value - gutterR.value - 24
  store.zoom.value = Math.max(0.25, Math.min(4, Math.round((room / (page.value.w * 96 / 25.4)) * 100) / 100))
}

function onWheel(e: WheelEvent) {
  if (!e.ctrlKey && !e.metaKey) return
  e.preventDefault()
  const z = store.zoom.value * (e.deltaY < 0 ? 1.1 : 1 / 1.1)
  store.zoom.value = Math.max(0.25, Math.min(4, Math.round(z * 100) / 100))
}

// ── rulers ──
function ticks(from: number, to: number, origin: number, labelEvery: number, vertical = false) {
  const p = ppm.value
  const minor = p >= 2.6 ? 1 : p >= 1.2 ? 2 : 5
  let d = ''
  const labels: { at: number; text: string }[] = []
  for (let mm = Math.ceil(from / minor) * minor; mm <= to + 1e-6; mm += minor) {
    const at = origin + mm * p
    const len = mm % 10 === 0 ? 9 : mm % 5 === 0 ? 6 : 3
    d += vertical ? `M${RULER} ${at.toFixed(1)}h-${len}` : `M${at.toFixed(1)} ${RULER}v-${len}`
    if (mm % labelEvery === 0) labels.push({ at, text: String(mm) })
  }
  return { d, labels }
}
const labelEvery = computed(() => (ppm.value * 10 >= 30 ? 10 : ppm.value * 20 >= 30 ? 20 : 50))
const hTicks = computed(() => ticks(-margins.value[3], page.value.w - margins.value[3], originX.value, labelEvery.value))
const vTicks = computed(() => BANDS.map((b) => {
  const r = ticks(0, bandHeight.value[b], 0, labelEvery.value, true)
  return { band: b, top: yOf(b, 0), height: bandHeight.value[b] * ppm.value, ...r }
}))

// the selected item's extent on the rulers
const selBox = computed(() => {
  const s = store.selected.value
  if (!s) return null
  return { band: s.band, ...boxOf(s.item) }
})

// ── problems, conflicts, the drop ──
const invalid = computed(() => store.itemsWithProblems.value)
const moving = ref<{ id: string; type: ItemType } | null>(null)
const refusedBand = ref<BandName | null>(null)
/** the band refused the item for its height, not its kind: said as such */
const refusedTall = ref(false)
const conflictIds = computed(() => {
  const out = new Set<string>()
  for (const c of rowConflicts(def.value.sections.body.items)) { out.add(c.table.id); out.add(c.other.id) }
  return out
})
interface DropHint { band: BandName; allowed: boolean; x: number; y: number; w: number; h: number; target: string | null }
const dropHint = ref<DropHint | null>(null)
const dragging = computed(() => !!moving.value || !!dropHint.value)

const tableRows = computed(() => def.value.sections.body.items.filter((i) => i.type === 'table'))
/** The items in the order the PDF draws them, what comes later on top: the header's and the
 *  footer's as listed, the body's by their top and then their left (the engine lays the body
 *  out row by row, backend/app/reports/model.py) — a rectangle under a text it frames is
 *  drawn first, here as there. */
const drawn = computed<Record<BandName, DItem[]>>(() => {
  const body = def.value.sections.body.items.map((it, i) => ({ it, i }))
    .sort((a, b) => a.it.y - b.it.y || a.it.x - b.it.x || a.i - b.i).map((x) => x.it)
  return { page_header: def.value.sections.page_header.items, body, page_footer: def.value.sections.page_footer.items }
})
const showRowZones = computed(() => dragging.value || store.selected.value?.item.type === 'table')

function bandAt(sheetMm: number): BandName {
  const top = bandTop.value
  if (sheetMm < top.body) return 'page_header'
  if (sheetMm >= top.page_footer) return 'page_footer'
  return 'body'
}

/** The pointer on the sheet, in mm from its top left corner. */
function sheetPoint(e: { clientX: number; clientY: number }): { x: number; y: number } {
  const r = sheet.value!.getBoundingClientRect()
  return { x: (e.clientX - r.left) / ppm.value, y: (e.clientY - r.top) / ppm.value }
}

// ── snapping: the edges and centres of the other items, the band's edges, the grid ──
const guides = ref<{ v: number[]; h: number[]; band: BandName } | null>(null)
const badge = ref<string>('')

function snapAxis(edges: number[], cands: number[], thr: number): { delta: number; at: number } | null {
  let best: { delta: number; at: number } | null = null
  for (const e of edges) {
    for (const c of cands) {
      const d = c - e
      if (Math.abs(d) <= thr && (!best || Math.abs(d) < Math.abs(best.delta))) best = { delta: d, at: c }
    }
  }
  return best
}

function candidates(band: BandName, except: string) {
  const xs = [0, W.value / 2, W.value]
  const ys = band === 'body' ? [0] : [0, bandHeight.value[band] / 2, bandHeight.value[band]]
  for (const o of def.value.sections[band].items) {
    if (o.id === except) continue
    const b = boxOf(o)
    xs.push(b.x, b.x + b.w / 2, b.x + b.w)
    ys.push(b.y, b.y + b.h / 2, b.y + b.h)
  }
  return { xs, ys }
}

// ── gestures ──
type Handle = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw' | 'p1' | 'p2'
interface Gesture {
  kind: 'move' | 'resize' | 'band'
  id?: string
  band: BandName
  handle?: Handle
  sx: number
  sy: number
  x0: number
  y0: number
  w0: number
  h0: number
  moved: boolean
  /** the stage's scroll when the gesture began: scrolling while dragging moves the item too */
  scx?: number
  scy?: number
}
let g: Gesture | null = null
let lastPointer: PointerEvent | null = null
let edgeTimer = 0

function begin(e: PointerEvent, gesture: Gesture) {
  if (e.button !== 0) return
  e.preventDefault()
  e.stopPropagation()
  g = { ...gesture, scx: stage.value?.scrollLeft ?? 0, scy: stage.value?.scrollTop ?? 0 }
  store.notice.value = ''
  stage.value?.focus({ preventScroll: true })
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  window.addEventListener('pointercancel', onUp)
}

function grab(e: PointerEvent, it: DItem, band: BandName) {
  store.select(it.id)
  begin(e, { kind: 'move', id: it.id, band, sx: e.clientX, sy: e.clientY, x0: it.x, y0: it.y, w0: it.w, h0: it.h, moved: false })
}

function grabHandle(e: PointerEvent, handle: Handle) {
  const s = store.selected.value
  if (!s) return
  const it = s.item
  begin(e, { kind: 'resize', id: it.id, band: s.band, handle, sx: e.clientX, sy: e.clientY, x0: it.x, y0: it.y, w0: it.w, h0: it.h, moved: false })
}

function grabBand(e: PointerEvent, band: 'page_header' | 'page_footer') {
  const h = def.value.sections[band].height
  begin(e, { kind: 'band', band, sx: e.clientX, sy: e.clientY, x0: 0, y0: h, w0: 0, h0: h, moved: false })
}

const thr = () => 5 / ppm.value
const snapGrid = (v: number) => (store.snap.value ? Math.round(v) : mm1(v))

function onMove(e: PointerEvent) {
  if (!g) return
  lastPointer = e
  const dx = (e.clientX - g.sx + (stage.value?.scrollLeft ?? 0) - (g.scx ?? 0)) / ppm.value
  const dy = (e.clientY - g.sy + (stage.value?.scrollTop ?? 0) - (g.scy ?? 0)) / ppm.value
  if (g.moved && !edgeTimer) edgeTimer = requestAnimationFrame(edgeScroll)
  if (!g.moved) {
    if (Math.hypot(e.clientX - g.sx, e.clientY - g.sy) < 3) return
    g.moved = true
    store.gesture.value = true
  }
  if (g.kind === 'band') return moveBand(dy)
  const hit = store.find(g.id!)
  if (!hit) return
  if (g.kind === 'move') moveItem(hit.item, dx, dy, e)
  else resizeItem(hit.item, dx, dy, e.shiftKey)
}

function moveItem(it: DItem, dx: number, dy: number, e: PointerEvent) {
  if (!g) return
  moving.value = { id: it.id, type: it.type }
  // into another band, when it can hold the item
  const target = bandAt(sheetPoint(e).y)
  refusedBand.value = null
  refusedTall.value = false
  if (target !== g.band) {
    const fits = target === 'body' || boxOf(it).h <= bandHeight.value[target] + 1e-6
    if (allowedIn(target, it.type) && fits) {
      const from = def.value.sections[g.band].items
      from.splice(from.indexOf(it), 1)
      def.value.sections[target].items.push(it)
      const shift = bandTop.value[g.band] - bandTop.value[target]
      g.y0 += shift
      g.band = target
      store.selectedBand.value = target
    } else {
      refusedBand.value = target
      refusedTall.value = allowedIn(target, it.type)
    }
  }
  const band = g.band
  const line = it.type === 'line'
  const ox = line ? Math.min(0, it.w) : 0
  const oy = line ? Math.min(0, it.h) : 0
  const bw = Math.abs(line ? it.w : it.w)
  const bh = Math.abs(line ? it.h : it.h)
  let nx = g.x0 + dx
  let ny = g.y0 + dy
  const { xs, ys } = candidates(band, it.id)
  const sx = snapAxis([nx + ox, nx + ox + bw / 2, nx + ox + bw], xs, thr())
  const sy = snapAxis([ny + oy, ny + oy + bh / 2, ny + oy + bh], ys, thr())
  nx = sx ? nx + sx.delta : snapGrid(nx)
  ny = sy ? ny + sy.delta : snapGrid(ny)
  nx = Math.max(-ox, Math.min(nx, W.value - bw - ox))
  ny = band === 'body' ? Math.max(-oy, ny) : Math.max(-oy, Math.min(ny, bandHeight.value[band] - bh - oy))
  it.x = mm1(nx)
  it.y = mm1(ny)
  guides.value = { v: sx ? [sx.at] : [], h: sy ? [sy.at] : [], band }
  badge.value = `${fmt(it.x)}, ${fmt(it.y)} mm`
}

function resizeItem(it: DItem, dx: number, dy: number, shift: boolean) {
  if (!g || !g.handle) return
  const band = g.band
  const room = band === 'body' ? null : bandHeight.value[band]
  const { xs, ys } = candidates(band, it.id)
  const sx = (v: number) => { const s = snapAxis([v], xs, thr()); return s ? { v: v + s.delta, at: s.at } : { v: snapGrid(v), at: null } }
  const sy = (v: number) => { const s = snapAxis([v], ys, thr()); return s ? { v: v + s.delta, at: s.at } : { v: snapGrid(v), at: null } }
  const v: number[] = []
  const h: number[] = []
  if (it.type === 'line') {
    // an end of the line follows the pointer; Shift keeps it level or upright
    const fixed = g.handle === 'p1' ? { x: g.x0 + g.w0, y: g.y0 + g.h0 } : { x: g.x0, y: g.y0 }
    const start = g.handle === 'p1' ? { x: g.x0, y: g.y0 } : { x: g.x0 + g.w0, y: g.y0 + g.h0 }
    let px = sx(start.x + dx)
    let py = sy(start.y + dy)
    if (shift) {
      if (Math.abs(px.v - fixed.x) > Math.abs(py.v - fixed.y)) py = { v: fixed.y, at: null }
      else px = { v: fixed.x, at: null }
    }
    const cx = Math.max(0, Math.min(px.v, W.value))
    const cy = Math.max(0, room != null ? Math.min(py.v, room) : py.v)
    if (g.handle === 'p1') {
      it.x = mm1(cx); it.y = mm1(cy); it.w = mm1(fixed.x - cx); it.h = mm1(fixed.y - cy)
    } else {
      it.x = mm1(fixed.x); it.y = mm1(fixed.y); it.w = mm1(cx - fixed.x); it.h = mm1(cy - fixed.y)
    }
    if (px.at != null) v.push(px.at)
    if (py.at != null) h.push(py.at)
    guides.value = { v, h, band }
    badge.value = `${fmt(Math.hypot(it.w, it.h))} mm`
    return
  }
  const min = 2
  let L = g.x0
  let T = g.y0
  let R = g.x0 + g.w0
  let B = g.y0 + g.h0
  const hd = g.handle
  if (hd.includes('w')) { const s = sx(L + dx); L = Math.min(s.v, R - min); if (s.at != null) v.push(s.at) }
  if (hd.includes('e')) { const s = sx(R + dx); R = Math.max(s.v, L + min); if (s.at != null) v.push(s.at) }
  if (it.type !== 'table') {
    if (hd.includes('n')) { const s = sy(T + dy); T = Math.min(s.v, B - min); if (s.at != null) h.push(s.at) }
    if (hd.includes('s')) { const s = sy(B + dy); B = Math.max(s.v, T + min); if (s.at != null) h.push(s.at) }
  }
  L = Math.max(0, L)
  R = Math.min(W.value, R)
  T = Math.max(0, T)
  if (room != null) B = Math.min(room, B)
  else if (B - T > bodyPage.value) B = T + bodyPage.value
  it.x = mm1(L)
  it.w = mm1(R - L)
  if (it.type !== 'table') {
    it.y = mm1(T)
    it.h = mm1(B - T)
  }
  guides.value = { v, h, band }
  badge.value = `${fmt(it.w)} × ${fmt(it.h)} mm`
}

function moveBand(dy: number) {
  if (!g) return
  const band = g.band as 'page_header' | 'page_footer'
  const lowest = def.value.sections[band].items.reduce((m, i) => Math.max(m, boxOf(i).y + boxOf(i).h), 0)
  const [top, , bottom] = margins.value
  const other = band === 'page_header' ? def.value.sections.page_footer.height : def.value.sections.page_header.height
  const most = page.value.h - top - bottom - other - 20
  const h = Math.max(Math.ceil(lowest * 10) / 10, Math.min(most, snapGrid(g.h0 + dy)))
  def.value.sections[band].height = mm1(Math.max(0, h))
  badge.value = `${fmt(def.value.sections[band].height)} mm`
}

/** Near an edge of the stage while dragging, the stage scrolls (the body grows under an
 *  item taken down), and the item follows the pointer as if it had moved. */
function edgeScroll() {
  edgeTimer = 0
  const el = stage.value
  const p = lastPointer
  if (!g || !g.moved || !el || !p) return
  const r = el.getBoundingClientRect()
  const EDGE = 32
  const speed = (over: number) => Math.round(Math.min(EDGE, Math.max(0, over)) / 2) + 2
  const vy = p.clientY > r.bottom - EDGE ? speed(p.clientY - (r.bottom - EDGE))
    : p.clientY < r.top + RULER + EDGE ? -speed(r.top + RULER + EDGE - p.clientY) : 0
  const vx = p.clientX > r.right - EDGE ? speed(p.clientX - (r.right - EDGE))
    : p.clientX < r.left + RULER + EDGE ? -speed(r.left + RULER + EDGE - p.clientX) : 0
  if (!vx && !vy) return
  const before = [el.scrollLeft, el.scrollTop]
  el.scrollBy(vx, vy)
  if (el.scrollLeft === before[0] && el.scrollTop === before[1]) return // at the end: the next move goes on
  edgeTimer = requestAnimationFrame(edgeScroll)
  onMove(p)
}

function onUp() {
  if (edgeTimer) { cancelAnimationFrame(edgeTimer); edgeTimer = 0 }
  lastPointer = null
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  window.removeEventListener('pointercancel', onUp)
  const done = g
  g = null
  if (done?.moved && done.kind === 'move' && done.id) {
    const hit = store.find(done.id)
    if (refusedBand.value && hit) {
      const band = t(`reportDesigner.bands.${refusedBand.value}`)
      store.notice.value = refusedTall.value
        ? t('reportDesigner.canvas.bandTooLow', { band, h: fmt(bandHeight.value[refusedBand.value]) })
        : t('reportDesigner.canvas.bandRefuses', { band })
    }
    if (hit && hit.band === 'body' && store.settleRow(hit.item)) {
      store.notice.value = t('reportDesigner.canvas.movedUnderTable')
    }
  }
  moving.value = null
  refusedBand.value = null
  refusedTall.value = false
  guides.value = null
  badge.value = ''
  store.gesture.value = false
}

function fmt(v: number): string {
  return ((Math.round(v * 10) / 10) || 0).toLocaleString(undefined, { maximumFractionDigits: 1 })
}

// ── clicks on the empty page ──
function pickBand(band: BandName) {
  store.selectedId.value = null
  store.selectedBand.value = band
}
function clearSelection() {
  store.selectedId.value = null
  store.selectedBand.value = null
}

// ── drops from the panels ──
function payloadOf(e: DragEvent) {
  return e.dataTransfer?.types.includes(DRAG_TYPE) ? dragged.value : null
}

function onDragOver(e: DragEvent) {
  const p = payloadOf(e)
  if (!p || !sheet.value) return
  e.preventDefault()
  const pt = sheetPoint(e)
  const band = bandAt(pt.y)
  const local = { x: snapGrid(pt.x - margins.value[3]), y: snapGrid(pt.y - bandTop.value[band]) }
  if (p.kind === 'field') {
    const table = band === 'body' ? tableAt(local.x, local.y) : null
    dropHint.value = table
      ? { band, allowed: true, x: table.x, y: table.y, w: table.w, h: table.h, target: table.id }
      : { band, allowed: band === 'body', x: local.x, y: local.y, w: 40, h: 7, target: null }
  } else if (p.kind === 'snippet') {
    dropHint.value = { band, allowed: true, x: local.x, y: local.y, w: p.w, h: 6, target: null }
  } else {
    const type: ItemType = p.type === 'matrix' ? 'table' : p.type
    const size = sizeOf(p.type)
    dropHint.value = { band, allowed: allowedIn(band, type), x: type === 'table' ? 0 : local.x, y: local.y, w: size.w, h: size.h, target: null }
  }
  if (e.dataTransfer) e.dataTransfer.dropEffect = dropHint.value.allowed ? 'copy' : 'none'
}

/** A band that cannot hold what is being moved or dropped. */
function refuses(b: BandName): boolean {
  if (b === 'body') return false
  if (refusedBand.value === b) return true
  if (moving.value && !allowedIn(b, moving.value.type)) return true
  const hint = dropHint.value
  if (!hint) return false
  if (hint.band === b) return !hint.allowed
  const p = dragged.value
  return !!p && p.kind !== 'snippet' && (p.kind === 'field' || !allowedIn(b, p.type === 'matrix' ? 'table' : p.type))
}

function onDragLeave(e: DragEvent) {
  const to = e.relatedTarget as Node | null
  if (!to || !stage.value?.contains(to)) dropHint.value = null
}

function sizeOf(kind: InsertKind) {
  const it = newItem(def.value, kind, { x: 0, y: 0 })
  return { w: it.w, h: it.h }
}

function tableAt(x: number, y: number): DItem | null {
  return def.value.sections.body.items.find((i) => i.type === 'table' && x >= i.x && x <= i.x + i.w && y >= i.y && y <= i.y + i.h) ?? null
}

function onDrop(e: DragEvent) {
  const p = payloadOf(e)
  const hint = dropHint.value
  dropHint.value = null
  dragged.value = null
  if (!p || !hint) return
  e.preventDefault()
  if (!hint.allowed) {
    toast.error(p.kind === 'field'
      ? t('reportDesigner.canvas.noFieldsInBand', { band: t(`reportDesigner.bands.${hint.band}`) })
      : t('reportDesigner.canvas.bandRefuses', { band: t(`reportDesigner.bands.${hint.band}`) }))
    return
  }
  place(p, hint)
}

/** An element, a field or a ready-made text put on the page at a point of a band. */
function place(p: DragPayload, at: { band: BandName; x: number; y: number; target: string | null }) {
  const d = def.value
  if (p.kind === 'field' && at.target) {
    const hit = store.find(at.target)
    if (hit?.item.type === 'table') {
      const tb = hit.item
      if (!tb.dataset) tb.dataset = p.dataset
      if (tb.dataset !== p.dataset) {
        toast.error(t('reportDesigner.canvas.otherDataset', { table: tb.id, dataset: tb.dataset }))
        return
      }
      if (tb.matrix) tb.columns.splice(0, tb.columns.length ? 1 : 0, { header: p.field.name, value: '' })
      else tb.columns.push(columnFor(p.field))
      store.select(tb.id)
      return
    }
  }
  let it: DItem
  if (p.kind === 'field') {
    const text = newItem(d, 'text', at) as DText
    text.value = `=${fieldRef(p.field.name)}`
    text.dataset = p.dataset
    it = text
  } else if (p.kind === 'snippet') {
    const text = newItem(d, 'text', at) as DText
    text.value = p.value
    text.w = p.w
    text.h = 6
    it = text
  } else {
    it = newItem(d, p.type, at)
  }
  store.keepInside(at.band, it)
  store.add(at.band, it)
  if (at.band === 'body' && store.settleRow(it)) store.notice.value = t('reportDesigner.canvas.movedUnderTable')
  if (it.type === 'chart') store.chartPicker.value = it.id
}
defineExpose({ fitWidth, place })

// ── the chart's kind, asked when one is placed ──
const pickerPos = ref<{ left: number; top: number } | null>(null)
watch(() => store.chartPicker.value, async (id) => {
  pickerPos.value = null
  if (!id) return
  await nextTick()
  const el = stage.value?.querySelector<HTMLElement>(`[data-item="${CSS.escape(id)}"]`)
  if (!el) return
  el.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  const r = el.getBoundingClientRect()
  pickerPos.value = { left: Math.min(r.left, window.innerWidth - 340), top: Math.min(r.top + 8, window.innerHeight - 260) }
  await nextTick()
  document.querySelector<HTMLElement>('.rdc-kinds button.on')?.focus()
})
const pickedKind = computed(() => {
  const hit = store.chartPicker.value ? store.find(store.chartPicker.value) : null
  return hit?.item.type === 'chart' ? hit.item.kind : null
})
function chooseKind(kind: ChartKind) {
  const hit = store.chartPicker.value ? store.find(store.chartPicker.value) : null
  if (hit?.item.type === 'chart') {
    hit.item.kind = kind
    if (kind === 'gauge' || kind === 'bullet') hit.item.category = hit.item.category || null
  }
  store.chartPicker.value = null
  stage.value?.focus({ preventScroll: true })
}
function closePicker() {
  store.chartPicker.value = null
  stage.value?.focus({ preventScroll: true })
}

// the selected item comes into view (a new one, one picked from a problem)
watch(() => store.selectedId.value, async (id) => {
  if (!id) return
  await nextTick()
  stage.value?.querySelector<HTMLElement>(`[data-item="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  // added with a click or a drag from the panel's elements or fields: the keys go on with the item
  const active = document.activeElement
  if (!active || active === document.body || active.closest('.rds-tile, .rds-snip, .rds-field')) stage.value?.focus({ preventScroll: true })
})

// ── the selection's frame and handles ──
const selFrame = computed(() => {
  const s = store.selected.value
  if (!s) return null
  const b = boxOf(s.item)
  return {
    left: (margins.value[3] + b.x) * ppm.value,
    top: (bandTop.value[s.band] + b.y) * ppm.value,
    width: b.w * ppm.value,
    height: b.h * ppm.value,
    type: s.item.type,
    line: s.item.type === 'line' ? s.item : null,
  }
})
const handles = computed<Handle[]>(() => {
  const f = selFrame.value
  if (!f) return []
  if (f.type === 'line') return ['p1', 'p2']
  if (f.type === 'table') return ['w', 'e']
  return ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w']
})
function handleStyle(h: Handle) {
  const f = selFrame.value!
  if (h === 'p1' || h === 'p2') {
    const l = f.line!
    const x = (h === 'p1' ? l.x : l.x + l.w) * ppm.value + margins.value[3] * ppm.value
    const y = (h === 'p1' ? l.y : l.y + l.h) * ppm.value + bandTop.value[store.selected.value!.band] * ppm.value
    return { left: `${x}px`, top: `${y}px` }
  }
  const x = h.includes('w') ? f.left : h.includes('e') ? f.left + f.width : f.left + f.width / 2
  const y = h.includes('n') ? f.top : h.includes('s') ? f.top + f.height : f.top + f.height / 2
  return { left: `${x}px`, top: `${y}px` }
}
const CURSORS: Record<Handle, string> = {
  n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize', ne: 'nesw-resize', sw: 'nesw-resize',
  nw: 'nwse-resize', se: 'nwse-resize', p1: 'crosshair', p2: 'crosshair',
}

// a band's height with the keyboard (its separator)
function bandKey(e: KeyboardEvent, band: 'page_header' | 'page_footer') {
  const step = e.shiftKey ? 5 : 1
  const delta = e.key === 'ArrowUp' ? -step : e.key === 'ArrowDown' ? step : 0
  if (!delta) return
  e.preventDefault()
  g = { kind: 'band', band, sx: 0, sy: 0, x0: 0, y0: 0, w0: 0, h0: def.value.sections[band].height, moved: true }
  moveBand(delta)
  g = null
  badge.value = ''
}

const bandLabel = (b: BandName) => t(`reportDesigner.bands.${b}`)
</script>

<template>
  <div
    ref="stage"
    class="rdc-stage"
    tabindex="0"
    :aria-label="$t('reportDesigner.canvas.label')"
    @wheel="onWheel"
    @dragenter="onDragOver"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <div class="rdc-grid" :style="{ width: `${areaW + RULER}px`, height: `${areaH + RULER}px` }">
      <div class="rdc-corner" aria-hidden="true" />
      <!-- the rulers: 0 where the margins start -->
      <svg class="rdc-hruler" :width="areaW" :height="RULER" aria-hidden="true">
        <rect :x="sheetLeft" y="0" :width="margins[3] * ppm" :height="RULER" class="rdc-rmargin" />
        <rect :x="originX + W * ppm" y="0" :width="margins[1] * ppm" :height="RULER" class="rdc-rmargin" />
        <rect v-if="selBox" :x="originX + selBox.x * ppm" y="0" :width="Math.max(1, selBox.w * ppm)" :height="RULER" class="rdc-rsel" />
        <path :d="hTicks.d" class="rdc-tick" />
        <text v-for="l in hTicks.labels" :key="l.at" :x="l.at + 2" y="10" class="rdc-rlabel">{{ l.text }}</text>
      </svg>
      <svg class="rdc-vruler" :width="RULER" :height="areaH" aria-hidden="true">
        <g v-for="b in vTicks" :key="b.band" :transform="`translate(0 ${b.top})`">
          <rect x="0" y="0" :width="RULER" :height="b.height" :class="b.band === 'body' ? 'rdc-rbody' : 'rdc-rband'" />
          <rect v-if="selBox && selBox.band === b.band" x="0" :y="selBox.y * ppm" :width="RULER" :height="Math.max(1, selBox.h * ppm)" class="rdc-rsel" />
          <path :d="b.d" class="rdc-tick" />
          <text v-for="l in b.labels" :key="l.at" x="3" :y="l.at + 10" class="rdc-rlabel">{{ l.text }}</text>
        </g>
      </svg>

      <div class="rdc-area" :style="{ width: `${areaW}px`, height: `${areaH}px` }" @pointerdown.self="clearSelection">
        <!-- the bands' names, in the margin of the desk -->
        <div
          v-for="b in BANDS"
          :key="`l-${b}`"
          class="rdc-blabel"
          :class="{ on: store.selectedBand.value === b && !store.selectedId.value }"
          :style="{ right: `${areaW - sheetLeft + LABEL_GAP}px`, top: `${labelTops[b]}px`, maxWidth: `${labelText + LABEL_PAD}px` }"
          role="button"
          tabindex="0"
          :aria-pressed="store.selectedBand.value === b && !store.selectedId.value"
          @pointerdown.prevent="pickBand(b)"
          @keydown.enter.prevent="pickBand(b)"
          @keydown.space.prevent="pickBand(b)"
        >
          <span class="rdc-bname">{{ bandLabel(b) }}</span>
          <span class="rdc-bsize">{{ sizeText(b) }}</span>
        </div>

        <div
          ref="sheet"
          class="rdc-sheet"
          :style="{ left: `${sheetLeft}px`, top: `${PAD_Y}px`, width: `${sheetW}px`, height: `${sheetH * ppm}px` }"
          @pointerdown.self="clearSelection"
        >
          <!-- the margins' frame -->
          <div
            class="rdc-frame"
            :style="{ left: `${margins[3] * ppm}px`, top: `${margins[0] * ppm}px`, width: `${W * ppm}px`,
                      height: `${(sheetH - margins[0] - margins[2]) * ppm}px` }"
            aria-hidden="true"
          />

          <!-- the bands, each with its items -->
          <div
            v-for="b in BANDS"
            :key="b"
            class="rdc-band"
            :class="[b, { refuse: refuses(b), picked: store.selectedBand.value === b && !store.selectedId.value }]"
            :style="{ left: `${margins[3] * ppm}px`, top: `${bandTop[b] * ppm}px`, width: `${W * ppm}px`, height: `${bandHeight[b] * ppm}px` }"
            :data-band="b"
            @pointerdown.self="pickBand(b)"
          >
            <span v-if="b !== 'body' && refuses(b)" class="rdc-refuse">
              {{ refusedBand === b && refusedTall ? $t('reportDesigner.canvas.bandTooLowShort') : $t('reportDesigner.canvas.bandHolds') }}
            </span>
            <!-- a table's row is its own: the room beside it, while one works near it -->
            <template v-if="b === 'body' && showRowZones">
              <template v-for="tb in tableRows" :key="`z-${tb.id}`">
                <div v-if="tb.x > 0.5" class="rdc-zone" :style="{ left: '0px', top: `${tb.y * ppm}px`, width: `${tb.x * ppm}px`, height: `${tb.h * ppm}px` }" />
                <div v-if="tb.x + tb.w < W - 0.5" class="rdc-zone" :style="{ left: `${(tb.x + tb.w) * ppm}px`, top: `${tb.y * ppm}px`, width: `${(W - tb.x - tb.w) * ppm}px`, height: `${tb.h * ppm}px` }" />
              </template>
            </template>
            <RdItem
              v-for="it in drawn[b]"
              :key="it.id"
              :item="it"
              :def="def"
              :ppm="ppm"
              :selected="store.selectedId.value === it.id"
              :invalid="invalid.has(it.id)"
              :conflict="b === 'body' && conflictIds.has(it.id)"
              :drop-target="dropHint?.target === it.id"
              :tag-left="(W - it.x + margins[1]) * ppm + 12"
              @grab="grab($event, it, b)"
              @pick="store.selectedId.value !== it.id && store.select(it.id)"
            />
            <!-- the drop's outline -->
            <div
              v-if="dropHint && dropHint.band === b && !dropHint.target"
              class="rdc-ghost"
              :class="{ no: !dropHint.allowed }"
              :style="{ left: `${dropHint.x * ppm}px`, top: `${dropHint.y * ppm}px`, width: `${dropHint.w * ppm}px`, height: `${Math.max(dropHint.h, 1) * ppm}px` }"
            />
          </div>

          <!-- the end of the first page, when the body goes on -->
          <div v-if="bodyDesign > bodyPage" class="rdc-break" :style="{ top: `${(bandTop.body + bodyPage) * ppm}px`, left: `${margins[3] * ppm}px`, width: `${W * ppm}px` }">
            <span>{{ $t('reportDesigner.canvas.pageEnds') }}</span>
          </div>

          <!-- the header's and footer's heights, dragged from their lower edge -->
          <div
            v-for="b in (['page_header', 'page_footer'] as const)"
            :key="`grip-${b}`"
            class="rdc-grip"
            :style="{ left: `${margins[3] * ppm}px`, top: `${(bandTop[b] + bandHeight[b]) * ppm - 4}px`, width: `${W * ppm}px` }"
            role="separator"
            aria-orientation="horizontal"
            tabindex="0"
            :aria-label="$t('reportDesigner.canvas.bandHeight', { band: bandLabel(b) })"
            :aria-valuenow="bandHeight[b]"
            aria-valuemin="0"
            @pointerdown="grabBand($event, b)"
            @keydown="bandKey($event, b)"
          >
            <span class="rdc-griptab" />
          </div>

          <!-- the alignment guides -->
          <template v-if="guides">
            <div
              v-for="x in guides.v"
              :key="`gv-${x}`"
              class="rdc-guide v"
              :style="{ left: `${(margins[3] + x) * ppm}px`, top: `${bandTop[guides.band] * ppm}px`, height: `${bandHeight[guides.band] * ppm}px` }"
            />
            <div
              v-for="y in guides.h"
              :key="`gh-${y}`"
              class="rdc-guide h"
              :style="{ top: `${(bandTop[guides.band] + y) * ppm}px`, left: `${margins[3] * ppm}px`, width: `${W * ppm}px` }"
            />
          </template>

          <!-- the selection's handles -->
          <template v-if="selFrame">
            <span
              v-for="h in handles"
              :key="h"
              class="rdc-handle"
              :class="{ end: h === 'p1' || h === 'p2' }"
              :style="{ ...handleStyle(h), cursor: CURSORS[h] }"
              @pointerdown="grabHandle($event, h)"
            />
            <span v-if="badge" class="rdc-badge" :style="{ left: `${selFrame.left}px`, top: `${selFrame.top + selFrame.height + 6}px` }">{{ badge }}</span>
          </template>
          <span v-else-if="badge" class="rdc-badge" :style="{ left: `${margins[3] * ppm}px`, top: '6px' }">{{ badge }}</span>
        </div>
      </div>
    </div>

    <!-- which chart: asked when one is placed -->
    <Teleport to="body">
      <div v-if="store.chartPicker.value && pickerPos" class="rdc-pickback" @pointerdown.self="closePicker" @keydown.esc="closePicker">
        <div class="rdc-kinds" role="dialog" aria-modal="true" :aria-label="$t('reportDesigner.canvas.whichChart')" :style="{ left: `${pickerPos.left}px`, top: `${pickerPos.top}px` }">
          <p class="rdc-ktitle">{{ $t('reportDesigner.canvas.whichChart') }}</p>
          <div class="rdc-kgrid">
            <button
              v-for="k in CHART_KINDS"
              :key="k"
              type="button"
              :class="{ on: pickedKind === k }"
              @click="chooseKind(k)"
            >
              <RdChartGlyph :kind="k" :size="34" />
              <span>{{ $t(`reportDesigner.kinds.${k}`) }}</span>
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.rdc-stage {
  position: relative; overflow: auto; min-height: 0; height: 100%; outline: none;
  background:
    radial-gradient(1100px 520px at 50% -10%, color-mix(in srgb, var(--accent) 6%, transparent), transparent 65%),
    var(--bg);
}
.rdc-stage:focus-visible { box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--accent) 45%, transparent); }
.rdc-grid { display: grid; grid-template-columns: 22px auto; grid-template-rows: 22px auto; }
.rdc-corner, .rdc-hruler, .rdc-vruler { box-sizing: border-box; }
.rdc-corner { position: sticky; top: 0; left: 0; z-index: 4; background: var(--panel); border-right: 1px solid var(--border-soft); border-bottom: 1px solid var(--border-soft); }
.rdc-hruler { position: sticky; top: 0; z-index: 3; display: block; background: var(--panel); border-bottom: 1px solid var(--border-soft); }
.rdc-vruler { position: sticky; left: 0; z-index: 3; display: block; background: var(--panel); border-right: 1px solid var(--border-soft); }
.rdc-tick { stroke: var(--muted); stroke-width: 1; opacity: 0.7; fill: none; }
.rdc-rlabel { fill: var(--muted); font: 500 9px/1 'IBM Plex Sans', system-ui, sans-serif; font-variant-numeric: tabular-nums; }
.rdc-rmargin { fill: var(--border-soft); }
.rdc-rband { fill: color-mix(in srgb, var(--accent) 7%, transparent); }
.rdc-rbody { fill: transparent; }
.rdc-rsel { fill: color-mix(in srgb, var(--accent) 28%, transparent); }
.rdc-area { position: relative; }

.rdc-blabel {
  position: absolute; display: flex; flex-direction: column; align-items: flex-end; gap: 2px; padding: 4px 8px 4px 10px;
  border-radius: 8px; cursor: pointer; transition: background 0.15s, color 0.15s; transform: translateY(-1px);
  box-sizing: border-box; text-align: right; overflow-wrap: anywhere;
}
.rdc-blabel:hover { background: var(--panel); }
.rdc-blabel:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.rdc-blabel.on { background: var(--tint-accent); }
.rdc-bname { font-size: 10.5px; line-height: 13px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }
.rdc-blabel.on .rdc-bname { color: var(--accent-hi); }
.rdc-bsize { font-size: 11px; line-height: 14px; color: var(--muted); opacity: 0.8; font-variant-numeric: tabular-nums; }

.rdc-sheet {
  position: absolute; background: #ffffff; border-radius: 2px;
  box-shadow: 0 1px 2px rgba(10, 15, 30, 0.18), 0 10px 32px rgba(10, 15, 30, 0.22);
}
.rdc-frame { position: absolute; border: 1px dashed rgba(59, 91, 219, 0.28); pointer-events: none; }
.rdc-band { position: absolute; }
.rdc-band.page_header, .rdc-band.page_footer { background: rgba(59, 91, 219, 0.035); }
.rdc-band.page_header { border-bottom: 1px dashed rgba(59, 91, 219, 0.35); }
.rdc-band.page_footer { border-top: 1px dashed rgba(59, 91, 219, 0.35); }
.rdc-band.picked { box-shadow: inset 0 0 0 1.5px color-mix(in srgb, var(--accent) 60%, transparent); }
.rdc-band.refuse {
  background: repeating-linear-gradient(-45deg, rgba(201, 42, 42, 0.07) 0 6px, rgba(201, 42, 42, 0.02) 6px 12px);
  box-shadow: inset 0 0 0 1.5px rgba(201, 42, 42, 0.5);
}
.rdc-refuse {
  position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); z-index: 2; pointer-events: none;
  font: 500 11px/1.3 'IBM Plex Sans', system-ui, sans-serif; color: #b02525; background: #fff; padding: 3px 8px;
  border-radius: 6px; border: 1px solid rgba(201, 42, 42, 0.4); white-space: nowrap; max-width: 95%; overflow: hidden; text-overflow: ellipsis;
}
.rdc-zone {
  position: absolute; pointer-events: none;
  background: repeating-linear-gradient(-45deg, rgba(59, 91, 219, 0.09) 0 5px, transparent 5px 10px);
}
.rdc-ghost { position: absolute; pointer-events: none; border: 1.5px dashed var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); border-radius: 2px; }
.rdc-ghost.no { border-color: #c92a2a; background: rgba(201, 42, 42, 0.08); }
.rdc-break { position: absolute; height: 0; border-top: 1px dashed rgba(107, 117, 144, 0.7); pointer-events: none; }
.rdc-break span {
  position: absolute; right: 0; top: -9px; font: 500 10px/1 'IBM Plex Sans', system-ui, sans-serif; color: #6b7590;
  background: #fff; padding: 2px 6px; border-radius: 6px; border: 1px solid #e3e6ec;
}
.rdc-grip { position: absolute; height: 8px; cursor: row-resize; z-index: 5; display: grid; place-items: center; outline: none; }
.rdc-griptab { width: 34px; height: 4px; border-radius: 2px; background: rgba(59, 91, 219, 0.35); opacity: 0; transition: opacity 0.15s, background 0.15s; }
.rdc-grip:hover .rdc-griptab, .rdc-grip:focus-visible .rdc-griptab { opacity: 1; background: var(--accent); }
.rdc-grip:hover, .rdc-grip:focus-visible { background: color-mix(in srgb, var(--accent) 14%, transparent); }
.rdc-guide { position: absolute; pointer-events: none; z-index: 6; }
.rdc-guide.v { width: 0; border-left: 1px solid #e64980; }
.rdc-guide.h { height: 0; border-top: 1px solid #e64980; }
.rdc-handle {
  position: absolute; z-index: 7; width: 9px; height: 9px; margin: -4.5px 0 0 -4.5px; border-radius: 2px;
  background: #ffffff; border: 1.5px solid var(--accent); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25); touch-action: none;
}
.rdc-handle.end { border-radius: 50%; width: 11px; height: 11px; margin: -5.5px 0 0 -5.5px; }
.rdc-badge {
  position: absolute; z-index: 8; pointer-events: none; white-space: nowrap;
  font: 500 11px/1 'IBM Plex Sans', system-ui, sans-serif; font-variant-numeric: tabular-nums;
  color: var(--on-accent); background: var(--accent); padding: 4px 7px; border-radius: 6px; box-shadow: var(--shadow-1);
}

.rdc-pickback { position: fixed; inset: 0; z-index: 2400; }
.rdc-kinds {
  position: fixed; width: 330px; padding: 10px; background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; box-shadow: var(--shadow-2);
}
.rdc-ktitle { margin: 2px 4px 8px; font-size: 12px; font-weight: 600; color: var(--muted); }
.rdc-kgrid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }
.rdc-kgrid button {
  flex-direction: column; gap: 4px; padding: 7px 4px 6px; font-size: 11px; color: var(--muted);
  background: transparent; border-color: transparent;
}
.rdc-kgrid button :deep(svg) { color: var(--accent); }
.rdc-kgrid button:hover { background: var(--panel-2); color: var(--text); box-shadow: none; }
.rdc-kgrid button.on { background: var(--tint-accent); color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
@media (prefers-reduced-motion: reduce) {
  .rdc-griptab, .rdc-blabel { transition: none; }
}
</style>
