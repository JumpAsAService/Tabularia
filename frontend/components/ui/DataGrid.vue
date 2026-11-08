<script setup lang="ts">
// The one data grid of the product: a step's preview in the flow editor, the Viewer's table,
// a datasource's panel, a SQL script's result, the assistant's tables.
//
// It draws only the rows in view (and a margin around them): a Viewer of thousands of rows
// and dozens of columns costs what the rows on screen cost. The columns get their widths
// once per result, measured in the page's own type on the longest values, so they do not
// move while the rows under them change and no value is cut below a column's cap. The
// header stays in place over its own scroll box.
//
// It is an ARIA grid with one active cell (one tab stop): the arrows, Home and End, Page Up
// and Page Down move it (composables/previewGrid.ts, moveCell), past the rows drawn too;
// Ctrl+C copies its exact value; the headers are its first row.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Ban, Check, CircleAlert, Copy, Filter, MousePointerClick, PaintBucket } from 'lucide-vue-next'
import type { ColumnStats, HistogramResult, PreviewResult, ProfileResult, TopValuesResult } from '~/composables/useApi'
import { readableNumber } from '~/composables/readableNumber'
import { usePopover } from '~/composables/usePopover'
import { isSuperseded } from '~/composables/usePreviewSlots'
import {
  ariaRowCount, columnDetail, exactText, gridColumnFormatters, histogramBins, histogramEdges, histogramHeights,
  isIntegerDtype, isNumericDtype, moveCell, nullBarWidth, profileBasisText, profileStep, shareText, shortDtype, toTsv, visibleWindow,
  type CellPos, type ColumnChanges, type ColumnMark, type GridColumn, type RowCount,
} from '~/composables/previewGrid'

const props = withDefaults(defineProps<{
  result: PreviewResult | null
  // no rows to show yet: the skeleton
  loading?: boolean
  // the rows on screen belong to what is being replaced: dimmed and busy until the new ones
  stale?: boolean
  error?: string
  // something to say instead of a table that is not an error (no preview after a Script node)
  note?: string
  // for who reads an answer, not who checks a step (the assistant's tables): thousands
  // grouped, the binary noise of a float gone
  readable?: boolean
  // the values exactly as they come, with no per-column decimals
  raw?: boolean
  // the rows-and-columns sentence, when the surface around says it (the editor's and the
  // Viewer's tab bar); left out, the grid says its own above the table
  summary?: string | null
  summaryId?: string
  // what the step changed against its input: marks in the headers
  changes?: ColumnChanges | null
  // the real row count, when the surface asked for it: the grid's aria-rowcount
  count?: RowCount | null
  // what to say when there is nothing at all
  empty?: string
  label?: string
  // the columns of what is being computed, when the page knows them already (the node's
  // last preview, a source's schema): the header names them at once, over a skeleton.
  // Not known: the header of the rows on screen dims with them.
  pendingColumns?: GridColumn[] | null
  // the step's column profile (POST /tasks/profile) and where it stands; with `columnDetail`
  // a click on a column's name (or Enter) opens the column's profile
  profile?: ProfileResult | null
  profileState?: 'none' | 'loading' | 'done' | 'failed'
  // why the profile failed, said in its place, with «Try again» (`profileRetry`)
  profileError?: string
  profileRetry?: () => void
  // an opened column's most frequent values, or a number's histogram (one query a column)
  columnDetail?: (column: string, ask: DetailAsk) => Promise<TopValuesResult | HistogramResult>
  // the profile is partial (the footer's nulls) until a column is opened: this asks the whole
  profileWhole?: () => void
  // what a value of the profile can do: keep only its rows or leave them out (the editor
  // adds a Filter step, the Viewer a filter), and the nulls be filled (the editor)
  // (`columns`: the only columns they apply to, all when left out)
  valueActions?: { filter: (column: string, value: unknown, exclude: boolean) => void; fillNulls?: (column: string) => void; columns?: string[] } | null
}>(), {
  summary: undefined, summaryId: undefined, changes: null, count: null, empty: undefined, label: undefined,
  pendingColumns: null, profile: null, profileState: 'none', profileError: undefined, profileRetry: undefined,
  columnDetail: undefined, profileWhole: undefined, valueActions: null,
})
export type DetailAsk = { kind: 'top' } | { kind: 'histogram'; lo: number; hi: number; bins: number }

const { t, locale } = useI18n()
const uid = `dg-${Math.random().toString(36).slice(2, 9)}`

// ── what is drawn: the result, or the header of what is being computed ──────────
const sameColumns = (a: GridColumn[], b: GridColumn[]) =>
  a.length === b.length && a.every((c, j) => c.name === b[j].name && c.dtype === b[j].dtype)
// the columns being computed are known and are not the ones on screen: their header, a skeleton under it
const pending = computed(() => {
  const next = props.pendingColumns
  if (!next?.length) return false
  if (props.loading && !props.result) return true
  return !!props.stale && !!props.result && !sameColumns(next, props.result.columns)
})
// the rows on screen are being replaced by rows of columns not known: the header dims too
const staleHead = computed(() => !!props.stale && !!props.result && !props.pendingColumns?.length)
const cols = computed<GridColumn[]>(() => (pending.value ? props.pendingColumns! : props.result?.columns ?? []))
const rows = computed<Record<string, unknown>[]>(() => (pending.value ? [] : props.result?.rows ?? []))
const isNum = (dtype: string) => isNumericDtype(dtype)

// one formatter per column, built once per result
const formatters = computed(() => (props.raw || props.readable ? null : gridColumnFormatters(cols.value, rows.value, locale.value)))
function text(v: unknown, name: string, dtype: string): string {
  if (v === null || v === undefined) return '∅'
  if (props.readable && typeof v === 'number') return readableNumber(v, dtype)
  const f = formatters.value?.[name]
  return f ? f(v) : exactText(v)
}
// the exact value whenever the cell shows it otherwise: rounded or grouped, or in a column
// whose longest values pass its cap (where a value may be cut)
function tip(v: unknown, name: string, dtype: string, j: number): string | undefined {
  if (v === null || v === undefined) return undefined
  const exact = exactText(v)
  const shown = text(v, name, dtype)
  return shown !== exact || cappedCols.value[j] ? exact : undefined
}

// A row's cells as drawn (text, tooltip, class), made the first time the row comes into
// view and kept for the result: scrolling back and forth only diffs, it formats nothing.
// A plain Map, not reactive: it follows the result through `cellsFor`'s dependency on it.
type Cell = { text: string; tip: string | undefined; cls: string }
// the columns whose longest values pass the cap (see computeWidths)
const cappedCols = ref<boolean[]>([])
let cellMemo = new Map<number, Cell[]>()
watch([() => props.result, formatters, () => props.readable, cappedCols, pending], () => { cellMemo = new Map() })
function cellsFor(i: number): Cell[] {
  void props.result // re-read when the result changes
  void cappedCols.value // and when the columns that take a tooltip do
  let cells = cellMemo.get(i)
  if (cells) return cells
  const row = rows.value[i] ?? {}
  cells = cols.value.map((c, j) => {
    const v = row[c.name]
    const nul = v === null || v === undefined
    return { text: text(v, c.name, c.dtype), tip: tip(v, c.name, c.dtype, j), cls: (nul ? 'null' : '') + (isNum(c.dtype) ? ' num' : '') }
  })
  cellMemo.set(i, cells)
  return cells
}

// ── the summary the grid says itself (the surfaces without a tab bar) ─────────────
const ownSummaryId = `${uid}-sum`
const ownSummary = computed(() => {
  if (props.summary !== undefined || !props.result) return null
  const n = props.result.rows.length.toLocaleString(locale.value)
  const rowsText = props.result.truncated ? t('dataGrid.first', { shown: n }) : t('dataGrid.rows', { n })
  return `${rowsText} · ${t('dataGrid.columns', { n: props.result.columns.length })}`
})
const hintId = `${uid}-hint`
const describedBy = computed(() => [props.summaryId ?? (ownSummary.value ? ownSummaryId : null), hintId].filter(Boolean).join(' '))
// the real total when it is known, -1 («unknown») while it is not: never the rows drawn
const rowCountAria = computed(() =>
  pending.value ? -1 : ariaRowCount(rows.value.length, !!props.result?.truncated, props.count ?? { state: 'none' }))

// ── marks: what the step changed ───────────────────────────────────────────────
function markOf(name: string): ColumnMark | undefined {
  return pending.value ? undefined : props.changes?.marks[name]
}
function markLabel(m: ColumnMark): string {
  return t(m.kind === 'new' ? 'dataGrid.markNew' : m.kind === 'renamed' ? 'dataGrid.markRenamed' : 'dataGrid.markRetyped')
}

// ── the column profile ───────────────────────────────────────────────────────
const profiling = computed(() => props.columnDetail !== undefined)
// a profile speaks for the rows on screen only: none while they are being replaced
function stats(name: string): ColumnStats | undefined {
  return props.stale || pending.value ? undefined : props.profile?.columns?.[name]
}
const fmtCount = (n: number) => n.toLocaleString(locale.value)
function nullsLine(st: ColumnStats): string {
  const share = shareText(st.nulls, st.of, locale.value)
  const sampled = !!props.profile?.sampled && !st.whole.includes('nulls')
  return sampled
    ? t('dataGrid.profileNullShareSample', { share, n: fmtCount(props.profile!.rows) })
    : t('dataGrid.profileNullShare', { share })
}
function headTitle(c: GridColumn): string {
  const m = markOf(c.name)
  const lines = [c.name, c.dtype]
  if (m?.kind === 'new') lines.push(t('dataGrid.markNewTitle'))
  if (m?.kind === 'renamed') lines.push(t('dataGrid.markRenamedTitle', { from: m.from }))
  if (m?.kind === 'retyped') lines.push(t('dataGrid.markRetypedTitle', { from: shortDtype(m.from) }))
  const st = stats(c.name)
  if (st && st.nulls > 0) lines.push(nullsLine(st))
  return lines.join('\n')
}
// the header's null bar: a grey track while the profile is asked (its place kept), nothing
// for a column with no nulls, the null-handling colour in proportion when there are some
function nullBar(name: string): 'wait' | 'on' | 'off' {
  const st = stats(name)
  if (st) return st.nulls > 0 ? 'on' : 'off'
  return props.profileState === 'loading' && !props.profile && !props.stale && !pending.value ? 'wait' : 'off'
}

// ── copy: a column's name, a cell's exact value, a selection's ──────────────────
const copied = ref('')
const copiedCell = ref('')
let copiedTimer: ReturnType<typeof setTimeout> | undefined
async function writeClipboard(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value)
    return true
  } catch {
    // no async clipboard (a page not served over https): the older way, through a field
    const back = document.activeElement as HTMLElement | null
    const area = document.createElement('textarea')
    area.value = value
    area.setAttribute('readonly', '')
    area.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0'
    document.body.appendChild(area)
    area.select()
    let ok = false
    try { ok = document.execCommand('copy') } catch { /* nothing to copy with */ }
    area.remove()
    back?.focus?.({ preventScroll: true })
    return ok
  }
}
async function copyName(name: string) {
  if (!(await writeClipboard(name))) return
  copied.value = name
  clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => { copied.value = ''; copiedCell.value = '' }, 1200)
}
async function copyActive() {
  const { r, c } = active.value
  const col = cols.value[c]
  if (!col) return
  if (r < 0) return copyName(col.name)
  if (!(await writeClipboard(exactText(rows.value[r]?.[col.name])))) return
  copiedCell.value = `${r}:${c}`
  clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => { copied.value = ''; copiedCell.value = '' }, 700)
}
// Selected cells are copied with their exact values, tab-separated, a line per row: what a
// spreadsheet pastes. A part of one text cell is copied as the browser selected it.
function onCopy(e: ClipboardEvent) {
  const sel = window.getSelection()
  const box = scroller.value
  if (!sel || sel.isCollapsed || !box || !e.clipboardData) return
  const cells = [...box.querySelectorAll<HTMLTableCellElement>('td[data-c]')].filter((td) => sel.containsNode(td, true))
  if (!cells.length) return
  if (cells.length === 1 && !cells[0].classList.contains('num')) return
  const byRow = new Map<number, unknown[]>()
  for (const td of cells) {
    const r = Number(td.dataset.r)
    const c = cols.value[Number(td.dataset.c)]
    if (!c) continue
    if (!byRow.has(r)) byRow.set(r, [])
    byRow.get(r)!.push(rows.value[r]?.[c.name])
  }
  e.clipboardData.setData('text/plain', toTsv([...byRow.values()]))
  e.preventDefault()
}

// ── the keyboard: one active cell, the only tab stop ───────────────────────────
const active = ref<CellPos>({ r: -1, c: 0 })
// moved by the keyboard last (not the pointer): the active cell's ring is drawn from a class,
// since Chromium gives no :focus-visible after a key with a modifier (Ctrl+Home, Ctrl+End)
const kbd = ref(false)
// an active cell scrolled out of the rows drawn: its column's header keeps the tab stop
const activeDrawn = computed(() => active.value.r < 0 || (active.value.r >= win.value.start && active.value.r < win.value.end))
const headTab = (j: number) => (active.value.c === j && (active.value.r < 0 || !activeDrawn.value) ? 0 : -1)
const cellTab = (i: number, j: number) => (active.value.r === i && active.value.c === j ? 0 : undefined)
function headButton(j: number): HTMLButtonElement | null {
  return scroller.value?.querySelector<HTMLButtonElement>(`thead th[aria-colindex="${j + 2}"] .cn`) ?? null
}
function cellEl(p: CellPos): HTMLElement | null {
  if (p.r < 0) return headButton(p.c)
  return scroller.value?.querySelector<HTMLElement>(`tbody tr[aria-rowindex="${p.r + 2}"] td[data-c="${p.c}"]`) ?? null
}
/** Into view by hand: the sticky header and the pinned row numbers would cover a cell the
 *  browser scrolled to an edge. Moves the rows drawn at once, so the cell exists after a tick. */
function reveal(p: CellPos) {
  const box = scroller.value
  if (!box) return
  if (p.r >= 0) {
    const top = headH.value + p.r * rowH.value
    let st = box.scrollTop
    if (top < st + headH.value) st = top - headH.value
    else if (top + rowH.value > st + box.clientHeight) st = top + rowH.value - box.clientHeight
    st = Math.max(0, st)
    if (Math.abs(st - box.scrollTop) > 0.5) {
      box.scrollTop = st
      scrollTop.value = box.scrollTop
    }
  }
  const th = headButton(p.c)?.closest('th') as HTMLElement | null
  if (th) {
    const left = th.offsetLeft - rnWidth.value
    const right = th.offsetLeft + th.offsetWidth - box.clientWidth
    if (box.scrollLeft > left) box.scrollLeft = Math.max(0, left)
    else if (box.scrollLeft < right) box.scrollLeft = right
  }
}
async function goTo(p: CellPos) {
  active.value = p
  reveal(p)
  await nextTick()
  cellEl(p)?.focus({ preventScroll: true })
}
function onGridKey(e: KeyboardEvent) {
  kbd.value = true
  const el = e.target as HTMLElement
  const inHead = !!el.closest?.('thead .cn')
  const inCell = !inHead && !!el.matches?.('td[data-c]')
  if ((!inHead && !inCell) || e.altKey) return
  const mod = e.ctrlKey || e.metaKey
  if (mod && !e.shiftKey && e.key.toLowerCase() === 'c') {
    const sel = window.getSelection()
    if (sel && !sel.isCollapsed && scroller.value?.contains(sel.anchorNode)) return // a selection: onCopy
    e.preventDefault()
    e.stopPropagation() // the editor's Ctrl+C copies nodes
    void copyActive()
    return
  }
  if (mod && e.key !== 'Home' && e.key !== 'End') return // the page's shortcuts (save, undo)
  if (e.key === 'Enter' && inCell && profiling.value) {
    e.preventDefault()
    openProfile(active.value.c)
    return
  }
  const page = Math.max(1, Math.floor((viewport.value - headH.value) / rowH.value) - 1)
  const to = moveCell(active.value, e.key, mod, rows.value.length, cols.value.length, page)
  if (!to) return
  e.preventDefault()
  e.stopPropagation()
  void goTo(to)
}
function onGridFocus(e: FocusEvent) {
  const td = (e.target as HTMLElement).closest?.('td[data-c]') as HTMLElement | null
  if (td) active.value = { r: Number(td.dataset.r), c: Number(td.dataset.c) }
}
// a click makes its cell the active one (not while selecting text, which the focus would end)
function onGridClick(e: MouseEvent) {
  const td = (e.target as HTMLElement).closest?.('td[data-c]') as HTMLElement | null
  if (!td) return
  const sel = window.getSelection()
  if (sel && !sel.isCollapsed) return
  active.value = { r: Number(td.dataset.r), c: Number(td.dataset.c) }
  nextTick(() => { if (td.isConnected) td.focus({ preventScroll: true }) })
}
function onHeadClick(j: number) {
  const c = cols.value[j]
  if (!c) return
  active.value = { r: -1, c: j }
  if (profiling.value) openProfile(j)
  else void copyName(c.name)
}

// ── the column under the pointer, tinted like the row (its <col>, one class) ──────
const hoverCol = ref(-1)
function onOver(e: MouseEvent) {
  const cell = (e.target as HTMLElement).closest?.('td, th') as HTMLTableCellElement | null
  hoverCol.value = cell ? cell.cellIndex : -1
  // a value wider than its cell without a tooltip of its own gets one, the first time it is pointed at
  if (cell?.tagName === 'TD' && cell.dataset.c !== undefined && !cell.title && cell.scrollWidth > cell.clientWidth) {
    const c = cols.value[Number(cell.dataset.c)]
    if (c) cell.title = exactText(rows.value[Number(cell.dataset.r)]?.[c.name])
  }
}

// ── which rows are drawn ───────────────────────────────────────────────────────
const scroller = ref<HTMLElement | null>(null)
const rowH = ref(26) // measured on the first row drawn
const headH = ref(41)
const viewport = ref(400)
const scrollTop = ref(0)
// scrolled sideways: the pinned row numbers cast a shadow on the columns under them
const scrolledX = ref(false)
const win = computed(() => visibleWindow(Math.max(0, scrollTop.value - headH.value), viewport.value, rowH.value, rows.value.length))
const shownRows = computed(() => {
  const out: number[] = []
  for (let i = win.value.start; i < win.value.end; i++) out.push(i)
  return out
})
const padTop = computed(() => win.value.start * rowH.value)
const padBottom = computed(() => Math.max(0, (rows.value.length - win.value.end) * rowH.value))

let frame = 0
function onScroll() {
  if (popOpen.value) popOpen.value = false // anchored to a header that moved
  if (frame) return
  frame = requestAnimationFrame(() => {
    frame = 0
    const el = scroller.value
    if (!el) return
    // read once a frame, nothing written to the layout: only a change of side flips a class
    const sx = el.scrollLeft > 0
    if (sx !== scrolledX.value) scrolledX.value = sx
    // only a move that changes the rows drawn re-renders (see visibleWindow's snap)
    const next = el.scrollTop
    const before = win.value
    const after = visibleWindow(Math.max(0, next - headH.value), viewport.value, rowH.value, rows.value.length)
    if (after.start !== before.start || after.end !== before.end) scrollTop.value = next
  })
}
function measure() {
  const el = scroller.value
  if (!el) return
  viewport.value = el.clientHeight || viewport.value
  const head = el.querySelector('thead') as HTMLElement | null
  if (head?.offsetHeight) headH.value = head.offsetHeight
  const row = el.querySelector('tbody tr[aria-rowindex]') as HTMLElement | null
  const h = row?.getBoundingClientRect().height
  if (h && Math.abs(h - rowH.value) > 0.5) rowH.value = h
}
let resize: ResizeObserver | undefined
let resizeFrame = 0
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') {
    // measured in the next frame: a re-render inside the observer's own delivery (rows drawn,
    // a scrollbar coming or going) is what browsers report as «ResizeObserver loop»
    resize = new ResizeObserver(() => {
      if (resizeFrame) return
      resizeFrame = requestAnimationFrame(() => { resizeFrame = 0; measure() })
    })
  }
})
watch(scroller, (el, old) => {
  if (old) resize?.unobserve(old)
  if (el) { resize?.observe(el); nextTick(measure) }
})
onBeforeUnmount(() => {
  resize?.disconnect()
  if (frame) cancelAnimationFrame(frame)
  if (resizeFrame) cancelAnimationFrame(resizeFrame)
  clearTimeout(copiedTimer)
})

// a new result with other columns starts from the top-left; the same columns (a parameter
// changed, the same step again) keep the place
let lastShape = ''
watch(cols, async (cs) => {
  const shape = cs.map((c) => c.name).join('\u0001')
  if (shape !== lastShape) {
    lastShape = shape
    active.value = { r: -1, c: 0 }
    scrollTop.value = 0
    scrolledX.value = false
    await nextTick()
    if (scroller.value) { scroller.value.scrollTop = 0; scroller.value.scrollLeft = 0 }
  } else {
    if (active.value.r >= rows.value.length) active.value = { r: rows.value.length - 1, c: active.value.c }
    await nextTick()
    if (scroller.value) scrollTop.value = scroller.value.scrollTop
  }
  measure()
})
watch(() => props.result, async () => {
  popOpen.value = false
  detail.value = null
  await nextTick()
  measure()
})

// ── the columns' widths, once per result ──────────────────────────────────────
// Measured in the page itself, in the cells' own type (IBM Plex Sans with tabular figures,
// which a canvas does not draw), on the ~8 longest values of each column plus a character
// of air and two pixels, so a value is never cut by a rounding; measured again when the
// fonts finish loading, so a first visit does not keep the widths of a fallback font.
// Capped as a cell is (28em at 12px): a column whose longest values pass the cap gives
// every value its tooltip. The same columns again (another node of the same shape, the
// rows arriving under a header named before them) never narrow: nothing jumps back.
const CAP = 336
const MIN = 44
const PAD = 19 // 9px each side + the rule
const AIR = 2
const CANDIDATES = 8
const SANS = '"IBM Plex Sans", system-ui, sans-serif'
let ctx: CanvasRenderingContext2D | null = null
function measureText(s: string, font: string): number {
  if (!ctx) {
    if (typeof document === 'undefined') return s.length * 7
    ctx = document.createElement('canvas').getContext('2d')
  }
  if (!ctx) return s.length * 7
  ctx.font = font
  return ctx.measureText(s).width
}
/** The k longest strings by characters: the few worth measuring in pixels. */
function longest(values: string[], k = CANDIDATES): string[] {
  const top: string[] = []
  let floor = -1 // the shortest length kept, once the list is full
  for (const s of values) {
    if (top.length === k && s.length <= floor) continue
    top.push(s)
    top.sort((a, b) => b.length - a.length)
    if (top.length > k) top.pop()
    if (top.length === k) floor = top[k - 1].length
  }
  return top
}
type Plan = { cells: string[]; name: string; type: string; mark: string }
/** One hidden table in the page, every candidate in it, read in one layout. */
function measureInPage(plans: Plan[]): { cells: number[]; head: number[]; ch: number } | null {
  if (typeof document === 'undefined' || !document.body) return null
  const table = document.createElement('table')
  table.className = 'data'
  table.setAttribute('aria-hidden', 'true')
  table.style.cssText = 'position:absolute;left:0;top:0;width:auto;visibility:hidden;pointer-events:none;contain:layout style'
  const th = table.createTHead().insertRow().appendChild(document.createElement('th'))
  const td = table.createTBody().insertRow().insertCell()
  td.style.cssText = 'max-width:none;overflow:visible'
  const block = (parent: HTMLElement, text: string, cls = '') => {
    const el = parent.appendChild(document.createElement(cls ? 'span' : 'div'))
    if (cls) el.className = cls
    el.style.cssText = 'display:block;width:max-content'
    el.textContent = text
    return el
  }
  const zero = block(td, '0')
  const cellEls = plans.map((p) => p.cells.map((c) => block(td, c)))
  const nameEls = plans.map((p) => block(th, p.name))
  const typeEls = plans.map((p) => {
    const el = block(th, p.type, 'ct')
    if (p.mark) {
      const mk = el.appendChild(document.createElement('span'))
      mk.style.font = `600 10.5px ${SANS}`
      mk.style.marginLeft = '4px' // the type line's gap (a flex row in the header)
      mk.textContent = `· ${p.mark}`
    }
    return el
  })
  document.body.appendChild(table)
  try {
    const w = (el: HTMLElement) => el.getBoundingClientRect().width
    return {
      ch: w(zero),
      cells: cellEls.map((els) => els.reduce((m, el) => Math.max(m, w(el)), 0)),
      head: plans.map((_, j) => Math.max(w(nameEls[j]) + 16, w(typeEls[j]) + 1)), // + the copied tick; a pixel for the type line's rounding
    }
  } finally {
    table.remove()
  }
}
const fontsEpoch = ref(0) // moves when a font finishes loading: the widths are measured again
const widths = ref<number[]>([])
let widthShape = ''
let refit = false // the fonts changed: the widths may narrow to the real type's
function computeWidths() {
  const rs = rows.value
  const plans: Plan[] = cols.value.map((c) => {
    let cells: string[]
    if (isNum(c.dtype) && !props.readable) {
      // the longest number is the largest or the smallest one (one count of decimals a column)
      let lo: number | null = null; let hi: number | null = null
      for (const r of rs) {
        const v = r[c.name]
        if (typeof v !== 'number') continue
        if (lo === null || v < lo) lo = v
        if (hi === null || v > hi) hi = v
      }
      cells = [lo, hi].filter((v) => v !== null).map((v) => text(v, c.name, c.dtype))
    } else {
      cells = longest(rs.map((r) => text(r[c.name], c.name, c.dtype)))
    }
    const m = markOf(c.name)
    return { cells, name: c.name, type: shortDtype(c.dtype), mark: m ? markLabel(m) : '' }
  })
  const got = measureInPage(plans)
  let ws: number[] = []; const capped: boolean[] = []
  plans.forEach((p, j) => {
    // no page to measure in (a render on the server): the canvas, in the font asked for
    const cell = got ? got.cells[j] + got.ch : Math.max(0, ...p.cells.map((c) => measureText(c, `400 12px ${SANS}`))) + 7
    const head = got ? got.head[j] : Math.max(measureText(p.name, `600 12px ${SANS}`) + 16, measureText(p.type, '400 11px ui-monospace, monospace') + (p.mark ? measureText(` · ${p.mark}`, `600 10.5px ${SANS}`) : 0))
    capped.push(cell > CAP)
    ws.push(Math.ceil(Math.min(CAP, Math.max(MIN, cell + (p.cells.length ? AIR : 0), head)) + PAD))
  })
  const shape = cols.value.map((c) => `${c.name}\u0002${c.dtype}`).join('\u0001')
  if (shape === widthShape && !refit && widths.value.length === ws.length) ws = ws.map((w, j) => Math.max(w, widths.value[j]))
  widthShape = shape
  refit = false
  widths.value = ws
  if (capped.join() !== cappedCols.value.join()) cappedCols.value = capped
}
// the marks as text: a new object with the same marks (the editor recomputes them) measures nothing
watch([() => props.result, cols, formatters, () => JSON.stringify(props.changes?.marks ?? {}), () => props.readable, fontsEpoch], computeWidths, { immediate: true })
function onFontsLoaded() { refit = true; fontsEpoch.value++ }
onMounted(() => {
  const fonts = typeof document !== 'undefined' ? document.fonts : undefined
  if (!fonts) return
  fonts.addEventListener?.('loadingdone', onFontsLoaded)
  if (fonts.status !== 'loaded') fonts.ready.then(onFontsLoaded, () => {})
})
onBeforeUnmount(() => {
  if (typeof document !== 'undefined') document.fonts?.removeEventListener?.('loadingdone', onFontsLoaded)
})
// the row numbers' width: of the rows on screen, or of the ones before while the next compute
const rnWidth = computed(() => Math.round(measureText(String(pending.value ? props.result?.rows.length || 100 : rows.value.length), `400 11px ${SANS}`) + 18))
const tableWidth = computed(() => widths.value.reduce((s, w) => s + w, rnWidth.value))

// ── the skeleton: the same columns as the last result (clamp 3–8), stable widths ──
const SK_WIDTHS = [64, 40, 88, 52, 72, 46, 60, 78]
const skCols = computed(() => Math.min(8, Math.max(3, props.result?.columns.length ?? 5)))
const skW = (r: number, c: number) => `${SK_WIDTHS[(r * 3 + c) % SK_WIDTHS.length]}px`

// ── a column's profile, in a small panel under its header ──────────────────────
const popOpen = ref(false)
const popCol = ref(-1)
const popEl = ref<HTMLElement | null>(null)
const popStyle = ref<Record<string, string>>({})
// a click on the same header closes it: the focus leaving the panel closed it a moment before
let closed = { col: -1, at: 0 }
// The focus goes to the panel itself, not to its first button: a second Enter must not copy
// the name. Esc gives it back to the header of the column shown (the arrows move it).
usePopover(popOpen, popEl, () => { closed = { col: popCol.value, at: performance.now() }; popOpen.value = false },
  { focus: 'panel', returnTo: () => headButton(popCol.value) })
const popColumn = computed(() => (popOpen.value ? cols.value[popCol.value] ?? null : null))
const popStats = computed(() => (popColumn.value ? stats(popColumn.value.name) ?? null : null))
// what the column shows past its figures, once the whole profile has come (a footer-only
// one does not know the distinct values yet)
const popKind = computed(() => {
  const c = popColumn.value
  const st = popStats.value
  if (!c || !st || !props.profile || props.profile.partial) return null
  return columnDetail(st, c.dtype, props.profile.rows)
})
type Detail = { col: string; kind: 'top' | 'histogram'; state: 'loading' | 'done' | 'failed'; res?: TopValuesResult | HistogramResult }
const detail = ref<Detail | null>(null)
// the column's name and type said when the arrows move the panel to another column
const popSaid = ref('')
watch(() => props.stale, (s) => { if (s) popOpen.value = false })
function place(j: number) {
  const th = headButton(j)?.closest('th') as HTMLElement | null
  if (!th) return
  const r = th.getBoundingClientRect()
  const W = 300
  const left = Math.max(8, Math.min(r.left, window.innerWidth - W - 8))
  const below = window.innerHeight - r.bottom - 12
  const above = r.top - 12
  popStyle.value = below >= 260 || below >= above
    ? { left: `${left}px`, top: `${r.bottom + 4}px`, maxHeight: `${Math.max(140, below)}px` }
    : { left: `${left}px`, bottom: `${window.innerHeight - r.top + 4}px`, maxHeight: `${Math.max(140, above)}px` }
}
function openProfile(j: number) {
  const c = cols.value[j]
  if (!c || !props.columnDetail) return
  if (popOpen.value && popCol.value === j) {
    popOpen.value = false
    // the focus given back to the name, as it closes, counts as leaving the panel: that is
    // not a click to swallow, the next one opens it again
    nextTick(() => { closed = { col: -1, at: 0 } })
    return
  }
  if (closed.col === j && performance.now() - closed.at < 400) return
  popCol.value = j
  popSaid.value = ''
  place(j)
  popOpen.value = true
  if (props.profileWhole && (!props.profile || props.profile.partial)) props.profileWhole()
}
/** ← and → with the panel open: the column beside, the panel open on it. */
async function moveProfile(j: number) {
  const c = cols.value[j]
  if (!c) return
  popCol.value = j
  active.value = { r: -1, c: j }
  reveal({ r: -1, c: j })
  await nextTick()
  place(j)
  popSaid.value = `${c.name}, ${shortDtype(c.dtype)}`
}
function onPopKey(e: KeyboardEvent) {
  const to = profileStep(e.key, { alt: e.altKey, ctrl: e.ctrlKey, meta: e.metaKey, shift: e.shiftKey }, popCol.value, cols.value.length)
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation() }
  if (to !== null) void moveProfile(to)
}
// The column's most frequent values or its histogram, asked once the whole profile says
// which (a key asks nothing): after the profile, never beside it on the worker.
watch(popKind, () => { void loadDetail() })
async function loadDetail() {
  const c = popColumn.value
  const st = popStats.value
  const kind = popKind.value
  if (!c || !st || !props.columnDetail || (kind !== 'top' && kind !== 'histogram')) return
  if (detail.value?.col === c.name && detail.value.kind === kind && detail.value.state !== 'failed') return
  let ask: DetailAsk = { kind: 'top' }
  if (kind === 'histogram') {
    const lo = Number(st.min); const hi = Number(st.max)
    ask = { kind: 'histogram', lo, hi, bins: histogramBins(lo, hi, isIntegerDtype(c.dtype)) }
  }
  detail.value = { col: c.name, kind, state: 'loading' }
  try {
    const res = await props.columnDetail(c.name, ask)
    if (detail.value?.col === c.name) detail.value = { col: c.name, kind, state: 'done', res }
  } catch (e) {
    if (detail.value?.col === c.name) detail.value = isSuperseded(e) ? null : { col: c.name, kind, state: 'failed' }
  }
}
const popDetail = computed(() => (popColumn.value && detail.value?.col === popColumn.value.name ? detail.value : null))
const popTop = computed(() => (popDetail.value?.kind === 'top' && popDetail.value.state === 'done' ? (popDetail.value.res as TopValuesResult) : null))
const popHist = computed(() => (popDetail.value?.kind === 'histogram' && popDetail.value.state === 'done' ? (popDetail.value.res as HistogramResult) : null))
function shown(v: unknown, c: GridColumn): string {
  if (v === null || v === undefined) return '—'
  if (v === '') return t('dataGrid.profileEmptyText')
  return text(v, c.name, c.dtype)
}
// «First 100,000 of 42,007,616 rows» / «All 764 rows»: once, under the column's name
const popBasis = computed(() => (props.profile ? profileBasisText(props.profile, props.count, t, fmtCount) : ''))
const popSampled = computed(() => !!props.profile?.sampled)
// a figure of every row, said so when the rest is of the first rows only
const exact = (st: ColumnStats, what: string) => popSampled.value && st.whole.includes(what)
const distinctText = computed(() => {
  const st = popStats.value
  if (!st || st.distinct == null) return ''
  const n = fmtCount(st.distinct)
  const estimated = props.profile?.distinct_exact === false
  if (popSampled.value) return t(estimated ? 'dataGrid.profileDistinctSampleAbout' : 'dataGrid.profileDistinctSample', { n })
  return estimated ? `≈ ${n}` : n
})
// a value's share of the rows seen (the nulls' of the rows they are counted on)
const sampleRows = computed(() => props.profile?.rows ?? 0)
const pctOf = (n: number, of: number) => shareText(n, of, locale.value)
const barOf = (n: number, of: number) => (of > 0 && n > 0 ? `${Math.max(2, Math.min(100, (n / of) * 100)).toFixed(1)}%` : '0%')
const histBars = computed(() => (popHist.value ? histogramHeights(popHist.value.counts) : []))
const histEdges = computed(() => {
  const h = popHist.value
  const c = popColumn.value
  if (!h || !c) return []
  return histogramEdges(h.lo, h.hi, h.counts.length, isIntegerDtype(c.dtype))
})
function histTitle(i: number): string {
  const c = popColumn.value!
  const e = histEdges.value[i]
  const n = popHist.value!.counts[i] ?? 0
  const from = text(e.from, c.name, c.dtype)
  const to = text(e.to, c.name, c.dtype)
  return from === to ? t('dataGrid.profileHistBarOne', { v: from, n: fmtCount(n) }, n) : t('dataGrid.profileHistBar', { from, to, n: fmtCount(n) }, n)
}
const histLabel = computed(() => {
  const h = popHist.value
  const c = popColumn.value
  if (!h || !c) return ''
  const n = h.counts.reduce((a, b) => a + b, 0)
  return t('dataGrid.profileHistLabel', { n: fmtCount(n), bins: h.counts.length, min: text(h.lo, c.name, c.dtype), max: text(h.hi, c.name, c.dtype) })
})
// the value actions of the column open, when the surface has some for it
const popActs = computed(() => {
  const a = props.valueActions
  const c = popColumn.value
  return a && c && (!a.columns || a.columns.includes(c.name)) ? a : null
})
function act(kind: 'keep' | 'exclude' | 'fill', value?: unknown) {
  const c = popColumn.value
  const a = popActs.value
  if (!c || !a) return
  popOpen.value = false
  if (kind === 'fill') a.fillNulls?.(c.name)
  else a.filter(c.name, value, kind === 'exclude')
}
</script>

<template>
  <div
    class="datagrid"
    :class="{ 'dg-stale': stale && !!result && !pending, 'dg-stale-head': staleHead, 'dg-profiled': profiling, 'dg-kbd': kbd }"
    tabindex="-1"
    role="region"
    :aria-label="label ?? $t('dataGrid.regionLabel')"
    :aria-busy="stale || loading ? 'true' : undefined"
  >
    <div v-if="loading && !result && !pending" class="dg-skeleton">
      <!-- the summary's line only where the grid says its own: the surfaces with a tab bar
           say it there, and the table must not jump up by a line when it arrives -->
      <div v-if="summary === undefined" class="pad"><span class="sk" style="width: 150px" /></div>
      <table class="data">
        <thead>
          <tr>
            <th v-for="c in skCols" :key="c">
              <span class="sk" :style="{ width: skW(0, c), height: '11px' }" /><br />
              <span class="sk" :style="{ width: '34px', height: '8px' }" />
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in 8" :key="r">
            <td v-for="c in skCols" :key="c"><span class="sk" :style="{ width: skW(r, c) }" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else-if="error && !pending" class="dg-state dg-error" role="alert">
      <CircleAlert :size="16" class="dg-state-icon" aria-hidden="true" />
      <span>{{ error }}</span>
    </div>
    <div v-else-if="note && !result && !pending" class="dg-state dg-note-state muted">
      <span>{{ note }}</span>
      <slot name="action" />
    </div>
    <div v-else-if="!result && !pending" class="dg-state dg-empty muted">
      <MousePointerClick :size="16" class="dg-state-icon" aria-hidden="true" />
      <span>{{ empty ?? $t('dataGrid.selectNode') }}</span>
    </div>
    <template v-else>
      <div v-if="note" class="pad dg-note">{{ note }}</div>
      <div v-if="ownSummary" :id="ownSummaryId" class="dg-summary">{{ ownSummary }}</div>
      <div class="dg-frame" :class="{ 'dg-sx': scrolledX }">
        <div
          ref="scroller"
          class="dg-scroll"
          @scroll.passive="onScroll"
          @copy="onCopy"
          @mouseover="onOver"
          @mouseleave="hoverCol = -1"
          @keydown="onGridKey"
          @focusin="onGridFocus"
          @click="onGridClick"
          @pointerdown="kbd = false"
        >
          <table
            class="data dg-table"
            role="grid"
            :style="{ width: `${tableWidth}px` }"
            :aria-label="label ?? $t('dataGrid.regionLabel')"
            :aria-rowcount="rowCountAria"
            :aria-colcount="cols.length + 1"
            :aria-describedby="describedBy"
          >
            <colgroup>
              <col :style="{ width: `${rnWidth}px` }" />
              <col v-for="(c, j) in cols" :key="c.name" :style="{ width: `${widths[j]}px` }" :class="{ hc: j + 1 === hoverCol }" />
            </colgroup>
            <thead>
              <tr role="row" aria-rowindex="1">
                <th class="rn" role="columnheader" aria-colindex="1"><span class="sr-only">{{ $t('dataGrid.rowNumber') }}</span></th>
                <th
                  v-for="(c, j) in cols"
                  :key="c.name"
                  role="columnheader"
                  :aria-colindex="j + 2"
                  :class="{ num: isNum(c.dtype), marked: !!markOf(c.name) }"
                  :title="headTitle(c)"
                >
                  <button
                    type="button"
                    class="cn"
                    :class="{ 'dg-act': active.r < 0 && active.c === j }"
                    :tabindex="headTab(j)"
                    :aria-describedby="hintId"
                    :aria-haspopup="profiling ? 'dialog' : undefined"
                    :aria-expanded="profiling ? popOpen && popCol === j : undefined"
                    @click="onHeadClick(j)"
                    @focus="active = { r: -1, c: j }"
                  >
                    <span class="cn-text">{{ c.name }}</span>
                    <Check v-if="copied === c.name" :size="11" class="cn-done" aria-hidden="true" />
                  </button>
                  <!-- the type, the step's mark, and the share of nulls when there is room for it
                       (a part that does not fit goes to a second line, which is not shown) -->
                  <span class="ct"><span class="ct-type">{{ shortDtype(c.dtype) }}</span><span v-if="markOf(c.name)" class="mk">· {{ markLabel(markOf(c.name)!) }}</span><span
                    v-if="profiling && nullBar(c.name) === 'on'"
                    class="ct-null"
                    aria-hidden="true"
                  >· {{ shareText(stats(c.name)!.nulls, stats(c.name)!.of, locale) }} ∅</span></span>
                  <template v-if="profiling">
                    <span class="nb" :class="nullBar(c.name)" aria-hidden="true"><span
                      v-if="nullBar(c.name) === 'on'"
                      class="nb-fill"
                      :style="{ width: nullBarWidth(stats(c.name)!.nulls, stats(c.name)!.of) }"
                    /></span>
                    <!-- said once, with the column, and only when there are nulls -->
                    <span v-if="nullBar(c.name) === 'on'" class="sr-only">, {{ nullsLine(stats(c.name)!) }}</span>
                  </template>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="padTop" class="spacer" aria-hidden="true"><td :colspan="cols.length + 1" :style="{ height: `${padTop}px` }" /></tr>
              <tr v-for="i in shownRows" :key="i" role="row" :aria-rowindex="i + 2" :class="{ even: i % 2 === 1 }">
                <td class="rn" role="rowheader" aria-colindex="1">{{ i + 1 }}</td>
                <td
                  v-for="(cell, j) in cellsFor(i)"
                  :key="j"
                  role="gridcell"
                  :aria-colindex="j + 2"
                  :data-r="i"
                  :data-c="j"
                  :class="[cell.cls, { 'dg-copied': copiedCell === `${i}:${j}`, 'dg-act': active.r === i && active.c === j }]"
                  :title="cell.tip"
                  :tabindex="cellTab(i, j)"
                >{{ cell.text }}</td>
              </tr>
              <tr v-if="padBottom" class="spacer" aria-hidden="true"><td :colspan="cols.length + 1" :style="{ height: `${padBottom}px` }" /></tr>
              <!-- the rows of what is being computed, under its header -->
              <template v-if="pending">
                <tr v-for="r in 8" :key="`sk${r}`" class="dg-skrow" aria-hidden="true">
                  <td class="rn" />
                  <td v-for="(c, j) in cols" :key="c.name" :class="{ num: isNum(c.dtype) }"><span class="sk" :style="{ width: skW(r, j) }" /></td>
                </tr>
              </template>
            </tbody>
          </table>
          <p v-if="!rows.length && !pending" class="dg-norows muted">{{ $t('dataGrid.noRows') }}</p>
        </div>
        <span :id="hintId" class="sr-only">{{ profiling ? $t('dataGrid.keysHintProfile') : $t('dataGrid.keysHint') }}</span>
      </div>
    </template>

    <Teleport to="body">
      <div
        v-if="popOpen && popColumn"
        ref="popEl"
        class="dg-pop"
        role="dialog"
        tabindex="-1"
        :aria-labelledby="`${uid}-pop-t`"
        :aria-describedby="`${uid}-pop-d`"
        :style="popStyle"
        @keydown="onPopKey"
      >
        <div class="dg-pop-head">
          <p :id="`${uid}-pop-t`" class="dg-pop-name">{{ popColumn.name }}</p>
          <span class="dg-pop-type">{{ shortDtype(popColumn.dtype) }}</span>
        </div>
        <span class="sr-only" aria-live="polite">{{ popSaid }}</span>
        <div :id="`${uid}-pop-d`">
          <!-- which rows the figures are of: once, at the top -->
          <p v-if="popStats && popBasis" class="dg-pop-basis">{{ popBasis }}</p>
          <dl v-if="popStats" class="dg-pop-stats">
            <div v-if="popStats.distinct != null">
              <dt>{{ $t('dataGrid.profileDistinct') }}</dt>
              <dd>{{ distinctText }}</dd>
            </div>
            <template v-if="'min' in popStats && popStats.min != null">
              <div>
                <dt>{{ $t('dataGrid.profileMin') }}</dt>
                <dd><span class="dg-pop-v">{{ shown(popStats.min, popColumn) }}</span><span v-if="exact(popStats, 'min')" class="dg-pop-all">{{ $t('dataGrid.profileAllRowsTag') }}</span></dd>
              </div>
              <div>
                <dt>{{ $t('dataGrid.profileMax') }}</dt>
                <dd><span class="dg-pop-v">{{ shown(popStats.max, popColumn) }}</span><span v-if="exact(popStats, 'max')" class="dg-pop-all">{{ $t('dataGrid.profileAllRowsTag') }}</span></dd>
              </div>
            </template>
            <div v-if="popStats.min_len != null">
              <dt>{{ $t('dataGrid.profileLength') }}</dt>
              <dd>{{ popStats.min_len === popStats.max_len
                ? $t('dataGrid.profileLengthOne', { n: fmtCount(popStats.min_len!) }, popStats.min_len!)
                : $t('dataGrid.profileLengthRange', { min: fmtCount(popStats.min_len!), max: fmtCount(popStats.max_len ?? 0) }) }}</dd>
            </div>
          </dl>
          <p v-if="popKind === 'key'" class="dg-pop-key">{{ popSampled ? $t('dataGrid.profileKeySample') : $t('dataGrid.profileKey') }}</p>
        </div>
        <template v-if="popStats">
          <!-- a number's spread: 20 bars between its min and max -->
          <figure v-if="popHist" class="dg-hist" role="img" :aria-label="histLabel">
            <div class="dg-hist-bars">
              <span v-for="(h, k) in histBars" :key="k" class="dg-hist-bar" :title="histTitle(k)"><span :style="{ height: `${h}%` }" /></span>
            </div>
            <div class="dg-hist-axis" aria-hidden="true">
              <span>{{ shown(popHist.lo, popColumn) }}</span><span>{{ shown(popHist.hi, popColumn) }}</span>
            </div>
          </figure>
          <p v-else-if="popDetail?.kind === 'histogram' && popDetail.state === 'loading'" class="dg-pop-wait">{{ $t('dataGrid.profileHistLoading') }}</p>
          <p v-else-if="popDetail?.kind === 'histogram' && popDetail.state === 'failed'" class="dg-pop-wait">
            {{ $t('dataGrid.profileDetailFailed') }}
            <button type="button" class="dg-pop-link" @click="loadDetail">{{ $t('dataGrid.tryAgain') }}</button>
          </p>
          <!-- the most frequent values, a bar of their share, and what to do with one -->
          <template v-if="popKind === 'top'">
            <p class="dg-pop-sub">{{ $t('dataGrid.profileTop') }}</p>
            <p v-if="!popDetail || popDetail.state === 'loading'" class="dg-pop-wait">{{ $t('dataGrid.profileTopLoading') }}</p>
            <p v-else-if="popDetail.state === 'failed'" class="dg-pop-wait">
              {{ $t('dataGrid.profileDetailFailed') }}
              <button type="button" class="dg-pop-link" @click="loadDetail">{{ $t('dataGrid.tryAgain') }}</button>
            </p>
            <p v-else-if="!popTop?.values.length" class="dg-pop-wait">{{ $t('dataGrid.profileTopNone') }}</p>
          </template>
          <ul v-if="(popTop?.values.length && popKind === 'top') || popStats.nulls > 0" class="dg-pop-top">
            <template v-if="popKind === 'top'">
              <li v-for="(v, k) in popTop?.values ?? []" :key="k" :style="{ '--share': barOf(v.count, sampleRows) }">
                <span class="dg-pop-v">{{ shown(v.value, popColumn) }}</span>
                <span class="dg-pop-n" :title="fmtCount(v.count)">{{ pctOf(v.count, sampleRows) }}</span>
                <span v-if="popActs" class="dg-pop-acts">
                  <button type="button" class="dg-pop-act" :aria-label="$t('dataGrid.profileKeepValue', { v: shown(v.value, popColumn) })" :title="$t('dataGrid.profileKeepTitle')" @click="act('keep', v.value)"><Filter :size="12" aria-hidden="true" /></button>
                  <button type="button" class="dg-pop-act" :aria-label="$t('dataGrid.profileExcludeValue', { v: shown(v.value, popColumn) })" :title="$t('dataGrid.profileExcludeTitle')" @click="act('exclude', v.value)"><Ban :size="12" aria-hidden="true" /></button>
                </span>
              </li>
            </template>
            <!-- the nulls, among the values when there are some -->
            <li v-if="popStats.nulls > 0" class="dg-pop-nulls" :style="{ '--share': barOf(popStats.nulls, popStats.of) }">
              <span class="dg-pop-v"><span aria-hidden="true">∅</span><span class="sr-only">{{ $t('dataGrid.profileNulls') }}</span><span v-if="exact(popStats, 'nulls')" class="dg-pop-all">{{ $t('dataGrid.profileAllRowsTag') }}</span></span>
              <span class="dg-pop-n" :title="fmtCount(popStats.nulls)">{{ pctOf(popStats.nulls, popStats.of) }}</span>
              <span v-if="popActs" class="dg-pop-acts">
                <template v-if="popActs.fillNulls">
                  <button type="button" class="dg-pop-act dg-pop-fill" :title="$t('dataGrid.profileFillTitle')" @click="act('fill')"><PaintBucket :size="12" aria-hidden="true" /> {{ $t('dataGrid.profileFill') }}</button>
                </template>
                <template v-else>
                  <button type="button" class="dg-pop-act" :aria-label="$t('dataGrid.profileKeepNulls')" :title="$t('dataGrid.profileKeepTitle')" @click="act('keep', null)"><Filter :size="12" aria-hidden="true" /></button>
                  <button type="button" class="dg-pop-act" :aria-label="$t('dataGrid.profileExcludeNulls')" :title="$t('dataGrid.profileExcludeTitle')" @click="act('exclude', null)"><Ban :size="12" aria-hidden="true" /></button>
                </template>
              </span>
            </li>
          </ul>
          <p v-if="profile?.partial && profileState === 'loading'" class="dg-pop-wait">{{ $t('dataGrid.profileLoading') }}</p>
          <p v-else-if="profile?.partial && profileState === 'failed'" class="dg-pop-wait">
            {{ profileError || $t('dataGrid.profileFailedWhy') }}
            <button v-if="profileRetry" type="button" class="dg-pop-link" @click="profileRetry">{{ $t('dataGrid.tryAgain') }}</button>
          </p>
        </template>
        <p v-else-if="profileState === 'loading' || profileState === 'none'" class="dg-pop-wait">{{ $t('dataGrid.profileLoading') }}</p>
        <p v-else class="dg-pop-wait">
          {{ profileError || (profile ? $t('dataGrid.profileNoColumn') : $t('dataGrid.profileFailedWhy')) }}
          <button v-if="profileRetry && !profile" type="button" class="dg-pop-link" @click="profileRetry">{{ $t('dataGrid.tryAgain') }}</button>
        </p>
        <div class="dg-pop-actions">
          <button type="button" class="mini" @click="copyName(popColumn.name)">
            <component :is="copied === popColumn.name ? Check : Copy" :size="12" aria-hidden="true" />
            {{ copied === popColumn.name ? $t('dataGrid.copiedName') : $t('dataGrid.copyName') }}
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.datagrid { height: 100%; min-height: 0; display: flex; flex-direction: column; --dg-bg: var(--panel); }
/* focused from outside (back to the preview after «Compute again»): a ring on the region */
.datagrid:focus { outline: none; }
.datagrid:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: 6px; }
.datagrid .dg-table { --dg-sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.dg-skeleton { overflow: hidden; }
.pad { padding: 8px; }
.dg-note { color: var(--muted); padding-bottom: 0; }
.dg-summary { font-variant-numeric: tabular-nums; font-size: 12px; padding: 7px 9px; color: var(--muted); flex: none; }
.dg-frame { position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column; }
.dg-scroll { flex: 1; min-height: 0; overflow: auto; border-radius: 6px; }
/* the rows of what is being replaced: still there, plainly not the answer. The header keeps
   its contrast when it names the columns being computed, and dims with the rows when it
   cannot */
.dg-table tbody, .dg-table thead th > * { transition: opacity var(--dur-2) ease-out; }
.dg-stale .dg-table tbody { opacity: 0.42; }
.dg-stale-head .dg-table thead th > * { opacity: 0.42; }
@media (prefers-reduced-motion: reduce) { .dg-table tbody, .dg-table thead th > * { transition: none; } }

.dg-table { table-layout: fixed; }
.data th.num, .data td.num { text-align: right; }
/* one line height for every row: a ∅ drawn from a fallback font made its row 3px taller */
.dg-table td { white-space: nowrap; line-height: 15px; }
/* the zebra by the row's own index: drawn rows change as it scrolls, their parity must not */
.dg-table tbody tr:nth-child(n) { background: transparent; }
.dg-table tbody tr.even:nth-child(n) { background: var(--row-even); }
.dg-table tbody tr[aria-rowindex]:nth-child(n):hover { background: var(--row-hover); }
.dg-table tr.spacer td { padding: 0; border: 0; }
/* the active cell, from the keyboard: its ring drawn from the grid's own state, since
   Chromium gives no :focus-visible after Ctrl+Home or Ctrl+End */
.dg-table td[data-c]:focus { outline: none; }
.dg-table td[data-c]:focus-visible,
.dg-kbd .dg-scroll:focus-within .dg-table td.dg-act { outline: 2px solid var(--accent); outline-offset: -2px; }
.dg-table td.dg-copied { background-image: linear-gradient(var(--tint-accent), var(--tint-accent)); }

/* the row numbers: pinned on the left while the columns scroll, ruled on their right like the
   header (an inset shadow: border-collapse drops a sticky cell's borders), and casting a
   shadow on the columns once they scroll under them */
.dg-table .rn {
  position: sticky; left: 0; z-index: 1;
  background: var(--dg-bg);
  color: var(--muted); font-size: 11px; text-align: right; padding-left: 6px; padding-right: 8px;
  font-variant-numeric: tabular-nums;
  box-shadow: inset -1px 0 0 var(--border);
  transition: box-shadow var(--dur-2) ease-out;
  overflow: visible; /* a cell clips: the cover below must reach past its padding box */
}
/* border-collapse paints a cell's background from the middle of its borders, and the
   collapsed left border stays where the cell was before it stuck: the first pixel of the
   pinned cell let the scrolled cells through. Covered from the box's edge inwards */
.dg-table .rn::before {
  content: ''; position: absolute; top: -1px; bottom: -1px; left: -1px; width: 3px;
  background: inherit; pointer-events: none;
}
.dg-sx .dg-table .rn { box-shadow: inset -1px 0 0 var(--border), var(--shadow-pin); }
.dg-table tbody tr.even td.rn { background-image: linear-gradient(var(--row-even), var(--row-even)); }
.dg-table tbody tr:hover td.rn { background-image: linear-gradient(var(--row-hover), var(--row-hover)); }
.dg-table thead th { z-index: 2; }
.dg-table thead th.rn { z-index: 3; background: var(--panel-2); box-shadow: inset 0 -1px 0 var(--border), inset -1px 0 0 var(--border); }
.dg-sx .dg-table thead th.rn { box-shadow: inset 0 -1px 0 var(--border), inset -1px 0 0 var(--border), var(--shadow-pin); }
@media (prefers-reduced-motion: reduce) { .dg-table .rn { transition: none; } }

/* the header: the name (a button: the profile, or a copy of the name) over the short type,
   the step's mark and the share of nulls */
.dg-table th { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-table th .cn {
  display: flex; align-items: center; gap: 4px; max-width: 100%;
  margin: 0; padding: 0; border: 0; background: none; box-shadow: none;
  font: inherit; color: inherit; text-align: inherit; cursor: copy;
}
.dg-profiled .dg-table th .cn { cursor: pointer; }
.dg-table th.num .cn { margin-left: auto; justify-content: flex-end; }
.dg-table th .cn:hover, .dg-table th .cn[aria-expanded="true"] { color: var(--accent-hi, var(--accent)); background: none; box-shadow: none; }
.dg-table th .cn:focus-visible,
.dg-kbd .dg-scroll:focus-within .dg-table th .cn.dg-act:focus { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
.cn-text { overflow: hidden; text-overflow: ellipsis; }
.cn-done { flex: none; color: var(--accent-2); }
.dg-table th .mk { font-family: var(--dg-sans, inherit); font-size: 10.5px; font-weight: 600; color: var(--accent-hi); }
/* the type's line: the type, the step's mark, the share of nulls; one line, a part with
   no room going to a second one that is not shown, never cut */
.dg-table th .ct { display: flex; flex-wrap: wrap; column-gap: 4px; height: 1.3em; overflow: hidden; }
.dg-table th.num .ct { justify-content: flex-end; }
.dg-table th .ct-type { flex: none; }
.dg-table th .ct-null { flex: none; color: var(--op-nulls-ink); }
/* the share of nulls: a hairline under the type, filled from the left in the null-handling
   colour (Fill nulls, Drop nulls); no line at all with no nulls; a grey track (its place
   kept, so the header does not grow) only while the profile is asked */
.dg-table th .nb {
  display: block; position: relative; height: 2px; margin-top: 4px; border-radius: 1px; overflow: hidden;
  background: color-mix(in srgb, var(--muted) 24%, transparent);
  visibility: hidden;
}
.dg-table th .nb.on, .dg-table th .nb.wait { visibility: visible; }
.dg-table th .nb-fill { display: block; height: 100%; background: var(--op-nulls-ink); }
.dg-table th .nb.wait::after {
  content: ''; position: absolute; inset: 0; transform: translateX(-100%);
  background: linear-gradient(90deg, transparent, var(--muted), transparent); opacity: 0.5;
  animation: sk-shimmer 1.1s infinite;
}
@media (prefers-reduced-motion: reduce) { .dg-table th .nb.wait::after { animation: none; display: none; } }

.dg-skrow td { border-color: transparent; }
.dg-skrow td.num .sk { margin-left: auto; display: block; }

.dg-norows { margin: 10px 12px; font-size: 12.5px; }

/* empty, note and error: said in the panel's place of the table, with an icon */
.dg-state { display: flex; align-items: flex-start; gap: 8px; margin: 12px; font-size: 12.5px; line-height: 1.5; }
.dg-state-icon { flex: none; margin-top: 2px; }
.dg-note-state { flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.dg-error {
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, transparent);
  border-radius: 8px;
  background: var(--tint-danger);
  color: var(--danger-text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* a column's profile: a small panel under its header (teleported: the grid's box clips) */
.dg-pop {
  position: fixed; z-index: 2600; width: 300px; overflow: auto;
  padding: 10px 12px; background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-2); font-size: 12px; color: var(--text);
  font-variant-numeric: tabular-nums;
}
.dg-pop:focus { outline: none; }
.dg-pop-head { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.dg-pop-name { margin: 0; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-pop-type { flex: none; font: 400 11px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); }
/* which rows: under the name, before any figure */
.dg-pop-basis { margin: 2px 0 8px; color: var(--muted); font-size: 11.5px; }
.dg-pop-stats { display: grid; gap: 3px; margin: 0; }
.dg-pop-stats > div { display: flex; justify-content: space-between; gap: 12px; min-width: 0; }
.dg-pop-stats dt { color: var(--muted); flex: none; }
.dg-pop-stats dd { margin: 0; min-width: 0; display: flex; justify-content: flex-end; align-items: baseline; gap: 6px; overflow: hidden; white-space: nowrap; }
.dg-pop-v { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* a figure of every row, beside one of the first rows: said small, never in colour */
.dg-pop-all { flex: none; color: var(--muted); font-size: 10.5px; }
.dg-pop-key { margin: 8px 0 0; color: var(--text); }
.dg-pop-sub { margin: 10px 0 3px; color: var(--muted); }
/* a value, its share in figures and as a bar under it, what to do with it */
.dg-pop-top { margin: 6px 0 0; padding: 0; list-style: none; display: grid; gap: 1px; }
.dg-pop-top li {
  position: relative; display: flex; align-items: center; gap: 8px; min-width: 0; min-height: 24px; padding: 0 0 3px;
}
.dg-pop-top li::after {
  content: ''; position: absolute; left: 0; bottom: 0; height: 2px; width: var(--share, 0%);
  border-radius: 1px; background: var(--accent-hi, var(--accent)); opacity: 0.75;
}
.dg-pop-top li.dg-pop-nulls::after { background: var(--op-nulls-ink); opacity: 1; }
.dg-pop-top .dg-pop-v { flex: 1; display: flex; align-items: baseline; gap: 6px; }
.dg-pop-n { flex: none; color: var(--muted); }
.dg-pop-acts { flex: none; display: inline-flex; gap: 2px; }
.dg-pop-act {
  display: inline-flex; align-items: center; justify-content: center; gap: 4px; min-width: 22px; height: 22px; padding: 0 4px;
  border: 1px solid transparent; border-radius: 6px; background: none; box-shadow: none; color: var(--muted); font-size: 11.5px;
}
.dg-pop-act:hover { color: var(--text); border-color: var(--border); background: var(--panel-2); box-shadow: none; }
.dg-pop-act:focus-visible { outline: 2px solid var(--accent); outline-offset: 0; }
.dg-pop-fill { padding: 0 6px; }
.dg-pop-wait { margin: 6px 0 0; color: var(--muted); }
.dg-pop-link {
  margin: 0 0 0 4px; padding: 0; border: 0; background: none; box-shadow: none; font: inherit; color: var(--accent-hi, var(--accent));
  text-decoration: underline; text-underline-offset: 3px; cursor: pointer;
}
.dg-pop-link:hover { background: none; box-shadow: none; color: var(--text); }
.dg-pop-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
/* the histogram: bars from the floor, the min and max under them */
.dg-hist { margin: 10px 0 0; }
.dg-hist-bars { display: flex; align-items: flex-end; gap: 1px; height: 44px; border-bottom: 1px solid var(--border); }
.dg-hist-bar { flex: 1; height: 100%; display: flex; align-items: flex-end; }
.dg-hist-bar > span { display: block; width: 100%; border-radius: 1px 1px 0 0; background: var(--accent-hi, var(--accent)); opacity: 0.75; }
.dg-hist-bar:hover > span { opacity: 1; }
.dg-hist-axis { display: flex; justify-content: space-between; gap: 12px; margin-top: 3px; color: var(--muted); font-size: 11px; }
.dg-hist-axis span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-pop-actions { display: flex; justify-content: flex-end; margin-top: 10px; }
.dg-pop-actions .mini { display: inline-flex; align-items: center; gap: 5px; }
</style>
