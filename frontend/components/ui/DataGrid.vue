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
import { Check, CircleAlert, Copy, MousePointerClick } from 'lucide-vue-next'
import type { ColumnStats, PreviewResult, ProfileResult, TopValuesResult } from '~/composables/useApi'
import { readableNumber } from '~/composables/readableNumber'
import { usePopover } from '~/composables/usePopover'
import { isSuperseded } from '~/composables/usePreviewSlots'
import {
  ariaRowCount, exactText, gridColumnFormatters, isNumericDtype, moveCell, nullBarWidth, profileBasis, shareText,
  shortDtype, toTsv, visibleWindow,
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
  // the step's column profile (POST /tasks/profile) and where it stands; with `topValues`
  // a click on a column's name (or Enter) opens the column's profile
  profile?: ProfileResult | null
  profileState?: 'none' | 'loading' | 'done' | 'failed'
  topValues?: (column: string) => Promise<TopValuesResult>
  // the profile is partial (the footer's nulls) until a column is opened: this asks the whole
  profileWhole?: () => void
}>(), {
  summary: undefined, summaryId: undefined, changes: null, count: null, empty: undefined, label: undefined,
  pendingColumns: null, profile: null, profileState: 'none', topValues: undefined, profileWhole: undefined,
})

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
const profiling = computed(() => props.topValues !== undefined)
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
  if (st) lines.push(nullsLine(st))
  return lines.join('\n')
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
  top.value = null
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
      mk.textContent = ` · ${p.mark}`
    }
    return el
  })
  document.body.appendChild(table)
  try {
    const w = (el: HTMLElement) => el.getBoundingClientRect().width
    return {
      ch: w(zero),
      cells: cellEls.map((els) => els.reduce((m, el) => Math.max(m, w(el)), 0)),
      head: plans.map((_, j) => Math.max(w(nameEls[j]) + 16, w(typeEls[j]))), // + the copied tick
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
usePopover(popOpen, popEl, () => { closed = { col: popCol.value, at: performance.now() }; popOpen.value = false })
const popColumn = computed(() => (popOpen.value ? cols.value[popCol.value] ?? null : null))
const popStats = computed(() => (popColumn.value ? stats(popColumn.value.name) ?? null : null))
const top = ref<{ col: string; state: 'loading' | 'done' | 'failed'; res?: TopValuesResult } | null>(null)
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
  if (!c || !props.topValues) return
  if (popOpen.value && popCol.value === j) { popOpen.value = false; return }
  if (closed.col === j && performance.now() - closed.at < 400) return
  popCol.value = j
  place(j)
  popOpen.value = true
  if (props.profileWhole && (!props.profile || props.profile.partial)) props.profileWhole()
  void loadTop(c)
}
async function loadTop(c: GridColumn) {
  const st = stats(c.name)
  if (!props.topValues || st?.kind === 'other' || (top.value?.col === c.name && top.value.state !== 'failed')) return
  top.value = { col: c.name, state: 'loading' }
  try {
    const res = await props.topValues(c.name)
    if (top.value?.col === c.name) top.value = { col: c.name, state: 'done', res }
  } catch (e) {
    if (top.value?.col === c.name) top.value = isSuperseded(e) ? null : { col: c.name, state: 'failed' }
  }
}
const popTop = computed(() => (popColumn.value && top.value?.col === popColumn.value.name ? top.value : null))
function shown(v: unknown, c: GridColumn): string {
  if (v === null || v === undefined) return '—'
  if (v === '') return t('dataGrid.profileEmptyText')
  return text(v, c.name, c.dtype)
}
const popNote = computed(() => {
  const p = props.profile
  const st = popStats.value
  if (!p || !st) return ''
  const n = fmtCount(p.rows)
  switch (profileBasis(p.sampled, st.whole)) {
    case 'all': return t('dataGrid.profileAll', { n }, p.rows)
    case 'sample': return t('dataGrid.profileSample', { n })
    case 'nulls': return t('dataGrid.profileSampleNulls', { n })
    default: return t('dataGrid.profileSampleRange', { n })
  }
})
</script>

<template>
  <div
    class="datagrid"
    :class="{ 'dg-stale': stale && !!result && !pending, 'dg-stale-head': staleHead, 'dg-profiled': profiling }"
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
                  <span class="ct"><span class="ct-type">{{ shortDtype(c.dtype) }}</span><span v-if="markOf(c.name)" class="mk"> · {{ markLabel(markOf(c.name)!) }}</span></span>
                  <template v-if="profiling">
                    <span class="nb" :class="{ on: !!stats(c.name) }" aria-hidden="true"><span
                      class="nb-fill"
                      :style="{ width: stats(c.name) ? nullBarWidth(stats(c.name)!.nulls, stats(c.name)!.of) : '0%' }"
                    /></span>
                    <span v-if="stats(c.name)" class="sr-only">, {{ nullsLine(stats(c.name)!) }}</span>
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
                  :class="[cell.cls, { 'dg-copied': copiedCell === `${i}:${j}` }]"
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
        :aria-labelledby="`${uid}-pop-t`"
        :style="popStyle"
      >
        <div class="dg-pop-head">
          <p :id="`${uid}-pop-t`" class="dg-pop-name">{{ popColumn.name }}</p>
          <span class="dg-pop-type">{{ shortDtype(popColumn.dtype) }}</span>
        </div>
        <template v-if="popStats">
          <dl class="dg-pop-stats">
            <div>
              <dt>{{ $t('dataGrid.profileNulls') }}</dt>
              <dd>{{ fmtCount(popStats.nulls) }} <span class="dg-pop-q">· {{ shareText(popStats.nulls, popStats.of, locale) }}</span></dd>
            </div>
            <div v-if="popStats.distinct != null">
              <dt>{{ $t('dataGrid.profileDistinct') }}</dt>
              <dd>≈ {{ fmtCount(popStats.distinct) }}</dd>
            </div>
            <template v-if="'min' in popStats">
              <div><dt>{{ $t('dataGrid.profileMin') }}</dt><dd class="dg-pop-v">{{ shown(popStats.min, popColumn) }}</dd></div>
              <div><dt>{{ $t('dataGrid.profileMax') }}</dt><dd class="dg-pop-v">{{ shown(popStats.max, popColumn) }}</dd></div>
            </template>
            <div v-if="popStats.min_len != null">
              <dt>{{ $t('dataGrid.profileLength') }}</dt>
              <dd>{{ popStats.min_len === popStats.max_len
                ? $t('dataGrid.profileLengthOne', { n: fmtCount(popStats.min_len!) }, popStats.min_len!)
                : $t('dataGrid.profileLengthRange', { min: fmtCount(popStats.min_len!), max: fmtCount(popStats.max_len ?? 0) }) }}</dd>
            </div>
          </dl>
          <template v-if="popStats.kind !== 'other'">
            <p class="dg-pop-sub">{{ $t('dataGrid.profileTop') }}</p>
            <p v-if="!popTop || popTop.state === 'loading'" class="dg-pop-wait">{{ $t('dataGrid.profileTopLoading') }}</p>
            <p v-else-if="popTop.state === 'failed'" class="dg-pop-wait">{{ $t('dataGrid.profileTopFailed') }}</p>
            <p v-else-if="!popTop.res?.values.length" class="dg-pop-wait">{{ $t('dataGrid.profileTopNone') }}</p>
            <ol v-else class="dg-pop-top">
              <li v-for="(v, k) in popTop.res!.values" :key="k">
                <span class="dg-pop-v">{{ shown(v.value, popColumn) }}</span>
                <span class="dg-pop-n">{{ fmtCount(v.count) }}</span>
              </li>
            </ol>
          </template>
          <p v-if="profile?.partial && profileState === 'loading'" class="dg-pop-wait">{{ $t('dataGrid.profileLoading') }}</p>
          <p class="dg-pop-note">{{ popNote }}</p>
        </template>
        <p v-else class="dg-pop-wait">
          {{ profileState === 'loading' || profileState === 'none' ? $t('dataGrid.profileLoading') : $t('dataGrid.profileFailed') }}
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

.dg-table { table-layout: fixed; min-width: 100%; }
.data th.num, .data td.num { text-align: right; }
/* one line height for every row: a ∅ drawn from a fallback font made its row 3px taller */
.dg-table td { white-space: nowrap; line-height: 15px; }
/* the zebra by the row's own index: drawn rows change as it scrolls, their parity must not */
.dg-table tbody tr:nth-child(n) { background: transparent; }
.dg-table tbody tr.even:nth-child(n) { background: var(--row-even); }
.dg-table tbody tr[aria-rowindex]:nth-child(n):hover { background: var(--row-hover); }
.dg-table tr.spacer td { padding: 0; border: 0; }
/* the active cell, from the keyboard */
.dg-table td[data-c]:focus { outline: none; }
.dg-table td[data-c]:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
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
.dg-table th .cn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
.cn-text { overflow: hidden; text-overflow: ellipsis; }
.cn-done { flex: none; color: var(--accent-2); }
.dg-table th .mk { font-family: var(--dg-sans, inherit); font-size: 10.5px; font-weight: 600; color: var(--accent-hi); }
/* the share of nulls: a hairline under the type, filled from the left; its place is kept
   while the profile is asked, so the header does not grow when it comes */
.dg-table th .nb {
  display: block; height: 2px; margin-top: 4px; border-radius: 1px; overflow: hidden;
  background: color-mix(in srgb, var(--muted) 24%, transparent);
  visibility: hidden;
}
.dg-table th .nb.on { visibility: visible; }
.dg-table th .nb-fill { display: block; height: 100%; background: var(--muted); }

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
.dg-pop-head { display: flex; align-items: baseline; gap: 8px; min-width: 0; margin-bottom: 8px; }
.dg-pop-name { margin: 0; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-pop-type { flex: none; font: 400 11px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); }
.dg-pop-stats { display: grid; gap: 3px; margin: 0; }
.dg-pop-stats > div { display: flex; justify-content: space-between; gap: 12px; min-width: 0; }
.dg-pop-stats dt { color: var(--muted); flex: none; }
.dg-pop-stats dd { margin: 0; min-width: 0; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-pop-q { color: var(--muted); }
.dg-pop-v { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-pop-sub { margin: 10px 0 3px; color: var(--muted); }
.dg-pop-top { margin: 0; padding: 0; list-style: none; display: grid; gap: 2px; }
.dg-pop-top li { display: flex; justify-content: space-between; gap: 12px; min-width: 0; }
.dg-pop-n { flex: none; color: var(--muted); }
.dg-pop-wait { margin: 2px 0; color: var(--muted); }
.dg-pop-note { margin: 10px 0 0; color: var(--muted); font-size: 11.5px; line-height: 1.4; white-space: normal; }
.dg-pop-actions { display: flex; justify-content: flex-end; margin-top: 10px; }
.dg-pop-actions .mini { display: inline-flex; align-items: center; gap: 5px; }
</style>
