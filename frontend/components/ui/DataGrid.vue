<script setup lang="ts">
// The one data grid of the product: a step's preview in the flow editor, the Viewer's table,
// a datasource's panel, a SQL script's result, the assistant's tables.
//
// It draws only the rows in view (and a margin around them): a Viewer of thousands of rows
// and dozens of columns costs what the rows on screen cost. The columns get their widths
// once per result, measured in the page's own type on the longest values, so they do not
// move while the rows under them change and no value is cut below a column's cap. The
// header stays in place over its own scroll box.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, CircleAlert, MousePointerClick } from 'lucide-vue-next'
import type { PreviewResult } from '~/composables/useApi'
import { readableNumber } from '~/composables/readableNumber'
import {
  ariaRowCount, exactText, gridColumnFormatters, isNumericDtype, shortDtype, toTsv, visibleWindow,
  type ColumnChanges, type ColumnMark, type RowCount,
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
}>(), { summary: undefined, summaryId: undefined, changes: null, count: null, empty: undefined, label: undefined })

const { t, locale } = useI18n()

const cols = computed(() => props.result?.columns ?? [])
const rows = computed(() => props.result?.rows ?? [])
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
watch([() => props.result, formatters, () => props.readable, cappedCols], () => { cellMemo = new Map() })
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
const ownSummaryId = `dg-sum-${Math.random().toString(36).slice(2, 9)}`
const ownSummary = computed(() => {
  if (props.summary !== undefined || !props.result) return null
  const n = props.result.rows.length.toLocaleString(locale.value)
  const rowsText = props.result.truncated ? t('dataGrid.first', { shown: n }) : t('dataGrid.rows', { n })
  return `${rowsText} · ${t('dataGrid.columns', { n: props.result.columns.length })}`
})
const describedBy = computed(() => props.summaryId ?? (ownSummary.value ? ownSummaryId : undefined))
// the real total when it is known, -1 («unknown») while it is not: never the rows drawn
const rowCountAria = computed(() => ariaRowCount(rows.value.length, !!props.result?.truncated, props.count ?? { state: 'none' }))

// ── marks: what the step changed ───────────────────────────────────────────────
function markOf(name: string): ColumnMark | undefined {
  return props.changes?.marks[name]
}
function markLabel(m: ColumnMark): string {
  return t(m.kind === 'new' ? 'dataGrid.markNew' : m.kind === 'renamed' ? 'dataGrid.markRenamed' : 'dataGrid.markRetyped')
}
function headTitle(c: { name: string; dtype: string }): string {
  const m = markOf(c.name)
  const lines = [c.name, c.dtype]
  if (m?.kind === 'new') lines.push(t('dataGrid.markNewTitle'))
  if (m?.kind === 'renamed') lines.push(t('dataGrid.markRenamedTitle', { from: m.from }))
  if (m?.kind === 'retyped') lines.push(t('dataGrid.markRetypedTitle', { from: shortDtype(m.from) }))
  return lines.join('\n')
}

// ── copy: a column's name, and cells with their exact values ───────────────────
const copied = ref('')
let copiedTimer: ReturnType<typeof setTimeout> | undefined
async function copyName(name: string) {
  try {
    await navigator.clipboard.writeText(name)
    copied.value = name
    clearTimeout(copiedTimer)
    copiedTimer = setTimeout(() => { copied.value = '' }, 1200)
  } catch { /* no clipboard (an insecure page): nothing to say */ }
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

// ── the header: one tab stop, the arrows move along the names ───────────────────
const activeHead = ref(0)
function headButton(j: number): HTMLButtonElement | null {
  return scroller.value?.querySelector<HTMLButtonElement>(`thead th[aria-colindex="${j + 2}"] .cn`) ?? null
}
function onHeadKey(e: KeyboardEvent, j: number) {
  const last = cols.value.length - 1
  const to = e.key === 'ArrowRight' ? j + 1 : e.key === 'ArrowLeft' ? j - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? last : null
  if (to === null) return
  e.preventDefault()
  const k = Math.max(0, Math.min(last, to))
  activeHead.value = k
  const btn = headButton(k)
  if (!btn) return
  btn.focus({ preventScroll: true })
  // into view by hand: the pinned row numbers would cover a column scrolled to the left edge
  const box = scroller.value
  const th = btn.closest('th') as HTMLElement | null
  if (box && th) {
    const left = th.offsetLeft - rnWidth.value
    const right = th.offsetLeft + th.offsetWidth - box.clientWidth
    if (box.scrollLeft > left) box.scrollLeft = Math.max(0, left)
    else if (box.scrollLeft < right) box.scrollLeft = right
  }
}

// ── the column under the pointer, tinted like the row (its <col>, one class) ──────
const hoverCol = ref(-1)
function onOver(e: MouseEvent) {
  const cell = (e.target as HTMLElement).closest?.('td, th') as HTMLTableCellElement | null
  hoverCol.value = cell ? cell.cellIndex : -1
}

// ── which rows are drawn ───────────────────────────────────────────────────────
const scroller = ref<HTMLElement | null>(null)
const rowH = ref(26) // measured on the first row drawn
const headH = ref(41)
const viewport = ref(400)
const scrollTop = ref(0)
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
  if (frame) return
  frame = requestAnimationFrame(() => {
    frame = 0
    const el = scroller.value
    if (!el) return
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
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') {
    resize = new ResizeObserver(() => { measure() })
  }
})
watch(scroller, (el, old) => {
  if (old) resize?.unobserve(old)
  if (el) { resize?.observe(el); nextTick(measure) }
})
onBeforeUnmount(() => {
  resize?.disconnect()
  if (frame) cancelAnimationFrame(frame)
  clearTimeout(copiedTimer)
})

// a new result with other columns starts from the top-left; the same columns (a parameter
// changed, the same step again) keep the place
let lastShape = ''
watch(() => props.result, async (r) => {
  const shape = r ? r.columns.map((c) => c.name).join('\u0001') : ''
  if (shape !== lastShape) {
    lastShape = shape
    activeHead.value = 0
    scrollTop.value = 0
    await nextTick()
    if (scroller.value) { scroller.value.scrollTop = 0; scroller.value.scrollLeft = 0 }
  } else {
    await nextTick()
    if (scroller.value) scrollTop.value = scroller.value.scrollTop
  }
  measure()
})

// ── the columns' widths, once per result ──────────────────────────────────────
// Measured in the page itself, in the cells' own type (IBM Plex Sans with tabular figures,
// which a canvas does not draw), on the ~8 longest values of each column plus a character
// of air; measured again when the fonts finish loading, so a first visit does not keep the
// widths of a fallback font. Capped as a cell is (28em at 12px): a column whose longest
// values pass the cap gives every value its tooltip.
const CAP = 336
const MIN = 44
const PAD = 19 // 9px each side + the rule
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
  const ws: number[] = []; const capped: boolean[] = []
  plans.forEach((p, j) => {
    // no page to measure in (a render on the server): the canvas, in the font asked for
    const cell = got ? got.cells[j] + got.ch : Math.max(0, ...p.cells.map((c) => measureText(c, `400 12px ${SANS}`))) + 7
    const head = got ? got.head[j] : Math.max(measureText(p.name, `600 12px ${SANS}`) + 16, measureText(p.type, '400 11px ui-monospace, monospace') + (p.mark ? measureText(` · ${p.mark}`, `600 10.5px ${SANS}`) : 0))
    capped.push(cell > CAP)
    ws.push(Math.ceil(Math.min(CAP, Math.max(MIN, cell, head)) + PAD))
  })
  widths.value = ws
  if (capped.join() !== cappedCols.value.join()) cappedCols.value = capped
}
// the marks as text: a new object with the same marks (the editor recomputes them) measures nothing
watch([() => props.result, formatters, () => JSON.stringify(props.changes?.marks ?? {}), () => props.readable, fontsEpoch], computeWidths, { immediate: true })
function onFontsLoaded() { fontsEpoch.value++ }
onMounted(() => {
  const fonts = typeof document !== 'undefined' ? document.fonts : undefined
  if (!fonts) return
  fonts.addEventListener?.('loadingdone', onFontsLoaded)
  if (fonts.status !== 'loaded') fonts.ready.then(onFontsLoaded, () => {})
})
onBeforeUnmount(() => {
  if (typeof document !== 'undefined') document.fonts?.removeEventListener?.('loadingdone', onFontsLoaded)
})
const rnWidth = computed(() => Math.round(measureText(String(rows.value.length), `400 11px ${SANS}`) + 18))
const tableWidth = computed(() => widths.value.reduce((s, w) => s + w, rnWidth.value))

// ── the skeleton: the same columns as the last result (clamp 3–8), stable widths ──
const SK_WIDTHS = [64, 40, 88, 52, 72, 46, 60, 78]
const skCols = computed(() => Math.min(8, Math.max(3, props.result?.columns.length ?? 5)))
const skW = (r: number, c: number) => `${SK_WIDTHS[(r * 3 + c) % SK_WIDTHS.length]}px`
</script>

<template>
  <div class="datagrid" :class="{ 'dg-stale': stale && !!result }" :aria-busy="stale || loading ? 'true' : undefined">
    <div v-if="loading && !result" class="dg-skeleton">
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
    <div v-else-if="error" class="dg-state dg-error" role="alert">
      <CircleAlert :size="16" class="dg-state-icon" aria-hidden="true" />
      <span>{{ error }}</span>
    </div>
    <div v-else-if="note && !result" class="dg-state muted">{{ note }}</div>
    <div v-else-if="!result" class="dg-state dg-empty muted">
      <MousePointerClick :size="16" class="dg-state-icon" aria-hidden="true" />
      <span>{{ empty ?? $t('dataGrid.selectNode') }}</span>
    </div>
    <template v-else>
      <div v-if="note" class="pad dg-note">{{ note }}</div>
      <div v-if="ownSummary" :id="ownSummaryId" class="dg-summary">{{ ownSummary }}</div>
      <div class="dg-frame">
        <div
          ref="scroller"
          class="dg-scroll"
          tabindex="0"
          role="region"
          :aria-label="label ?? $t('dataGrid.regionLabel')"
          @scroll.passive="onScroll"
          @copy="onCopy"
          @mouseover="onOver"
          @mouseleave="hoverCol = -1"
        >
          <table
            class="data dg-table"
            :style="{ width: `${tableWidth}px` }"
            :aria-rowcount="rowCountAria"
            :aria-colcount="cols.length + 1"
            :aria-describedby="describedBy"
          >
            <colgroup>
              <col :style="{ width: `${rnWidth}px` }" />
              <col v-for="(c, j) in cols" :key="c.name" :style="{ width: `${widths[j]}px` }" :class="{ hc: j + 1 === hoverCol }" />
            </colgroup>
            <thead>
              <tr aria-rowindex="1">
                <th class="rn" scope="col" aria-colindex="1"><span class="sr-only">{{ $t('dataGrid.rowNumber') }}</span></th>
                <th
                  v-for="(c, j) in cols"
                  :key="c.name"
                  scope="col"
                  :aria-colindex="j + 2"
                  :class="{ num: isNum(c.dtype), marked: !!markOf(c.name) }"
                  :title="headTitle(c)"
                >
                  <button
                    type="button"
                    class="cn"
                    :tabindex="j === activeHead ? 0 : -1"
                    :aria-label="$t('dataGrid.copyName', { name: c.name })"
                    @click="copyName(c.name)"
                    @focus="activeHead = j"
                    @keydown="onHeadKey($event, j)"
                  >
                    <span class="cn-text">{{ c.name }}</span>
                    <Check v-if="copied === c.name" :size="11" class="cn-done" aria-hidden="true" />
                  </button>
                  <span class="ct"><span class="ct-type">{{ shortDtype(c.dtype) }}</span><span v-if="markOf(c.name)" class="mk"> · {{ markLabel(markOf(c.name)!) }}</span></span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="padTop" class="spacer" aria-hidden="true"><td :colspan="cols.length + 1" :style="{ height: `${padTop}px` }" /></tr>
              <tr v-for="i in shownRows" :key="i" :aria-rowindex="i + 2" :class="{ even: i % 2 === 1 }">
                <td class="rn" aria-hidden="true">{{ i + 1 }}</td>
                <td
                  v-for="(cell, j) in cellsFor(i)"
                  :key="j"
                  :data-r="i"
                  :data-c="j"
                  :class="cell.cls"
                  :title="cell.tip"
                >{{ cell.text }}</td>
              </tr>
              <tr v-if="padBottom" class="spacer" aria-hidden="true"><td :colspan="cols.length + 1" :style="{ height: `${padBottom}px` }" /></tr>
            </tbody>
          </table>
          <p v-if="!rows.length" class="dg-norows muted">{{ $t('dataGrid.noRows') }}</p>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.datagrid { height: 100%; min-height: 0; display: flex; flex-direction: column; --dg-bg: var(--panel); }
.datagrid .dg-table { --dg-sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.dg-skeleton { overflow: hidden; }
.pad { padding: 8px; }
.dg-note { color: var(--muted); padding-bottom: 0; }
.dg-summary { font-variant-numeric: tabular-nums; font-size: 12px; padding: 7px 9px; color: var(--muted); flex: none; }
.dg-frame { position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column; }
.dg-scroll { flex: 1; min-height: 0; overflow: auto; border-radius: 6px; }
/* the region's focus ring is drawn over the table, not under the pinned row numbers and
   header that would cover an outline */
.dg-scroll:focus-visible { outline: none; }
.dg-frame:has(> .dg-scroll:focus-visible)::after {
  content: ''; position: absolute; inset: 0; z-index: 4; pointer-events: none;
  border: 2px solid var(--accent); border-radius: 6px;
}
@supports not selector(:has(*)) {
  .dg-scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
}
/* the rows of what is being replaced: still there, plainly not the answer. Only the body:
   the header keeps its contrast, the columns are still the ones being computed */
.dg-table tbody { transition: opacity var(--dur-2) ease-out; }
.dg-stale .dg-table tbody { opacity: 0.42; }
@media (prefers-reduced-motion: reduce) { .dg-table tbody { transition: none; } }

.dg-table { table-layout: fixed; min-width: 100%; }
.data th.num, .data td.num { text-align: right; }
.dg-table td { white-space: nowrap; }
/* the zebra by the row's own index: drawn rows change as it scrolls, their parity must not */
.dg-table tbody tr:nth-child(n) { background: transparent; }
.dg-table tbody tr.even:nth-child(n) { background: var(--row-even); }
.dg-table tbody tr[aria-rowindex]:nth-child(n):hover { background: var(--row-hover); }
.dg-table tr.spacer td { padding: 0; border: 0; }

/* the row numbers: pinned on the left while the columns scroll */
.dg-table .rn {
  position: sticky; left: 0; z-index: 1;
  background: var(--dg-bg);
  color: var(--muted); font-size: 11px; text-align: right; padding-left: 6px; padding-right: 8px;
  font-variant-numeric: tabular-nums;
}
.dg-table tbody tr.even td.rn { background-image: linear-gradient(var(--row-even), var(--row-even)); }
.dg-table tbody tr:hover td.rn { background-image: linear-gradient(var(--row-hover), var(--row-hover)); }
.dg-table thead th { z-index: 2; }
.dg-table thead th.rn { z-index: 3; background: var(--panel-2); }

/* the header: the name (a button that copies it) over the short type and the step's mark */
.dg-table th { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-table th .cn {
  display: flex; align-items: center; gap: 4px; max-width: 100%;
  margin: 0; padding: 0; border: 0; background: none; box-shadow: none;
  font: inherit; color: inherit; text-align: inherit; cursor: copy;
}
.dg-table th.num .cn { margin-left: auto; justify-content: flex-end; }
.dg-table th .cn:hover { color: var(--accent-hi, var(--accent)); background: none; box-shadow: none; }
.dg-table th .cn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
.cn-text { overflow: hidden; text-overflow: ellipsis; }
.cn-done { flex: none; color: var(--accent-2); }
.dg-table th .mk { font-family: var(--dg-sans, inherit); font-size: 10.5px; font-weight: 600; color: var(--accent); }

.dg-norows { margin: 10px 12px; font-size: 12.5px; }

/* empty, note and error: said in the panel's place of the table, with an icon */
.dg-state { display: flex; align-items: flex-start; gap: 8px; margin: 12px; font-size: 12.5px; line-height: 1.5; }
.dg-state-icon { flex: none; margin-top: 2px; }
.dg-error {
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, transparent);
  border-radius: 8px;
  background: var(--tint-danger);
  color: var(--danger-text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
