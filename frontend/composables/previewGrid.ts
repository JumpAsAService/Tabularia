// What the data grid (components/ui/DataGrid.vue) and the surfaces around it compute, with
// nothing of Nuxt or the DOM: the tests import it as it is (tests/preview-grid.test.mjs).
//
// - how a column's values are written (the same decimals on a whole column, the exact
//   value kept for the tooltip and the copy) and how its type is named, short;
// - which columns a step added, renamed, retyped or removed against the step before it;
// - the one sentence that says how many rows a preview shows of how many there are, the
//   same in the editor, in the Viewer and on a node of the canvas;
// - which rows a scrolled grid draws (it draws only those).

export interface GridColumn { name: string; dtype: string }

// ── types ─────────────────────────────────────────────────────────────────────
const NUMERIC = /^(U?Int(8|16|32|64|128)|Float(32|64)|Decimal)\b/
export const isNumericDtype = (dtype: string): boolean => NUMERIC.test(dtype || '')

/** «Datetime(time_unit='us', time_zone=None)» → «Datetime(us)»: the type as a person reads it;
 *  the engine's own name stays in the header's tooltip. */
export function shortDtype(dtype: string): string {
  const s = String(dtype || '').trim()
  const m = /^(\w+)\((.*)\)$/s.exec(s)
  if (!m) return s
  const [, kind, inner] = m
  switch (kind) {
    case 'Datetime': {
      const unit = /time_unit='(\w+)'/.exec(inner)?.[1]
      const zone = /time_zone='([^']+)'/.exec(inner)?.[1]
      return unit ? `Datetime(${unit}${zone ? `, ${zone}` : ''})` : 'Datetime'
    }
    case 'Duration': {
      const unit = /time_unit='(\w+)'/.exec(inner)?.[1]
      return unit ? `Duration(${unit})` : 'Duration'
    }
    case 'Decimal': {
      const p = /precision=(\w+)/.exec(inner)?.[1]
      const sc = /scale=(\d+)/.exec(inner)?.[1]
      return p && p !== 'None' ? `Decimal(${p}, ${sc ?? 0})` : 'Decimal'
    }
    case 'List': return `List(${shortDtype(inner)})`
    case 'Array': return `Array(${shortDtype(inner.split(/,\s*shape=/)[0])})`
    case 'Struct': case 'Categorical': case 'Enum': case 'Object': return kind
    default: return s
  }
}

// ── values ────────────────────────────────────────────────────────────────────
/** The value as it is: what the tooltip shows and what a copy takes. */
export function exactText(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

const numberFormats = new Map<string, Intl.NumberFormat>()
function numberFormat(locale: string | undefined, opts: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${locale ?? ''}${JSON.stringify(opts)}`
  let f = numberFormats.get(key)
  if (!f) numberFormats.set(key, (f = new Intl.NumberFormat(locale, opts)))
  return f
}

// how many values a column's decimals are decided on: enough for a preview, bounded for a
// Viewer of thousands of rows
const DECIMALS_SAMPLE = 2000

/**
 * The formatter of a data grid's numeric column: the same decimals on the whole column (the
 * most seen, up to 4) and integers WITHOUT a thousands separator — a code or a YYYYMM period
 * grouped («202.607») reads as a quantity. The decimals keep the language's separator.
 */
export function gridColumnFormatter(values: unknown[], locale?: string): (v: unknown) => string {
  let decimals = 0
  let integersOnly = true
  const n = Math.min(values.length, DECIMALS_SAMPLE)
  for (let i = 0; i < n; i++) {
    const v = values[i]
    if (typeof v !== 'number' || !Number.isFinite(v) || Number.isInteger(v)) continue
    integersOnly = false
    const s = String(v)
    // 1e-7 has 7 decimals, not «-7»: an exponent means «as many as allowed»
    const frac = s.includes('e') ? 4 : (s.split('.')[1]?.length ?? 0)
    decimals = Math.max(decimals, Math.min(4, frac))
    if (decimals === 4) break
  }
  const f = integersOnly
    ? numberFormat(locale, { useGrouping: false, maximumFractionDigits: 0 })
    : numberFormat(locale, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
  return (v) => (typeof v === 'number' && Number.isFinite(v) ? f.format(v) : exactText(v))
}

/** A formatter per column of a result: numeric columns aligned on their decimals, the others
 *  as they are. */
export function gridColumnFormatters(columns: GridColumn[], rows: Record<string, unknown>[], locale?: string): Record<string, (v: unknown) => string> {
  const out: Record<string, (v: unknown) => string> = {}
  for (const c of columns) {
    out[c.name] = isNumericDtype(c.dtype)
      ? gridColumnFormatter(rows.map((r) => r[c.name]), locale)
      : exactText
  }
  return out
}

/** Rows of cells as text a spreadsheet pastes: tabs between cells, a line per row. A cell
 *  with a tab, a line break or a quote is quoted, as spreadsheets expect. */
export function toTsv(rows: unknown[][]): string {
  const cell = (v: unknown) => {
    const s = exactText(v)
    return /[\t\n\r"]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  return rows.map((r) => r.map(cell).join('\t')).join('\n')
}

// ── what a step changed ───────────────────────────────────────────────────────
export type ColumnMark = { kind: 'new' } | { kind: 'renamed'; from: string } | { kind: 'retyped'; from: string }
export interface ColumnChanges { marks: Record<string, ColumnMark>; removed: string[] }

/**
 * The columns a step added, renamed or retyped against its input, and the ones it dropped.
 * `upstream` null (a source, or an input not known yet): nothing to compare, no marks.
 * `renames` old → new: a Rename step's mapping, the only way to tell a rename from a
 * column dropped and another added.
 */
export function columnChanges(
  upstream: GridColumn[] | null | undefined,
  current: GridColumn[],
  renames: Record<string, string> = {},
): ColumnChanges {
  if (!upstream || !upstream.length) return { marks: {}, removed: [] }
  const before = new Map(upstream.map((c) => [c.name, c.dtype]))
  const now = new Set(current.map((c) => c.name))
  const renamedFrom = new Map<string, string>()
  for (const [from, to] of Object.entries(renames || {})) {
    if (to && before.has(from) && !now.has(from) && now.has(to)) renamedFrom.set(String(to), from)
  }
  const marks: Record<string, ColumnMark> = {}
  for (const c of current) {
    const from = renamedFrom.get(c.name)
    if (from !== undefined) marks[c.name] = { kind: 'renamed', from }
    else if (!before.has(c.name)) marks[c.name] = { kind: 'new' }
    else if (before.get(c.name) !== c.dtype) marks[c.name] = { kind: 'retyped', from: before.get(c.name)! }
  }
  const gone = new Set(renamedFrom.values())
  const removed = upstream.map((c) => c.name).filter((n) => !now.has(n) && !gone.has(n))
  return { marks, removed }
}

// ── how many rows ─────────────────────────────────────────────────────────────
// The real count, asked apart (POST /tasks/count): `rows` up to the cap, `over` past it.
export type RowCount =
  | { state: 'none' }
  | { state: 'counting' }
  | { state: 'done'; rows: number; over: boolean }
  | { state: 'failed'; slow: boolean }

type T = (key: string, params?: Record<string, unknown>) => string

/**
 * The one way the product says how many rows a preview shows: «764 rows», «First 100 of
 * 764 rows», «First 100 of more than 10,000,000 rows», «First 100 rows» while the total is
 * not known. `fmt` writes a count in the interface's language.
 */
export function rowsPhrase(shown: number, truncated: boolean, count: RowCount, t: T, fmt: (n: number) => string): string {
  if (!truncated) return t('dataGrid.rows', { n: fmt(shown) })
  if (count.state === 'done') {
    if (count.over) return t('dataGrid.firstOfMore', { shown: fmt(shown), n: fmt(count.rows) })
    if (count.rows > shown) return t('dataGrid.firstOf', { shown: fmt(shown), n: fmt(count.rows) })
  }
  return t('dataGrid.first', { shown: fmt(shown) })
}

/** The same, short, for a node of the canvas: «764 rows», «more than 10,000,000 rows», «first 100 rows». */
export function nodeRowsPhrase(shown: number, truncated: boolean, count: RowCount, t: T, fmt: (n: number) => string): string {
  if (!truncated) return t('dataGrid.rows', { n: fmt(shown) })
  if (count.state === 'done') {
    if (count.over) return t('dataGrid.moreThan', { n: fmt(count.rows) })
    if (count.rows > shown) return t('dataGrid.rows', { n: fmt(count.rows) })
  }
  return t('dataGrid.firstShort', { shown: fmt(shown) })
}

/** Is the step big enough to suggest a sample on its source? Over a million rows, or a
 *  preview that made the person wait. */
export const SAMPLE_HINT_ROWS = 1_000_000
export const SAMPLE_HINT_MS = 5_000
export function suggestSample(count: RowCount, previewMs: number): boolean {
  if (previewMs >= SAMPLE_HINT_MS) return true
  if (count.state === 'failed' && count.slow) return true
  return count.state === 'done' && (count.over || count.rows > SAMPLE_HINT_ROWS)
}

// ── which rows to draw ────────────────────────────────────────────────────────
/**
 * The rows a grid scrolled to `scrollTop` draws: the visible ones and `overscan` on each
 * side. The start moves in steps of `snap` rows, so scrolling re-renders once every few
 * rows instead of at every pixel.
 */
export function visibleWindow(scrollTop: number, viewport: number, rowHeight: number, total: number,
  overscan = 8, snap = 8): { start: number; end: number } {
  if (total <= 0 || rowHeight <= 0) return { start: 0, end: 0 }
  const first = Math.max(0, Math.floor(Math.max(0, scrollTop) / rowHeight) - overscan)
  const start = Math.min(Math.floor(first / snap) * snap, Math.max(0, total - 1))
  const visible = Math.ceil(Math.max(viewport, rowHeight) / rowHeight)
  const end = Math.min(total, start + visible + overscan * 2 + snap)
  return { start, end }
}

// ── the previews a page has already seen ──────────────────────────────────────
/** A small memory of recent answers, the least recently used one forgotten first. */
export class Recent<V> {
  private map = new Map<string, V>()
  private size: number
  constructor(size = 40) { this.size = size }
  get(key: string): V | undefined {
    const v = this.map.get(key)
    if (v !== undefined) { this.map.delete(key); this.map.set(key, v) }
    return v
  }
  set(key: string, v: V): void {
    this.map.delete(key)
    this.map.set(key, v)
    while (this.map.size > this.size) this.map.delete(this.map.keys().next().value as string)
  }
  delete(key: string): void { this.map.delete(key) }
  clear(): void { this.map.clear() }
  get length(): number { return this.map.size }
}

/** The identity of a preview: the same engine, source, chain and limit give the same answer. */
export function previewKey(body: { engine?: string | null; bucket: string; input_key: string; operations: unknown[]; limit?: number }): string {
  return JSON.stringify([body.engine ?? '', body.bucket, body.input_key, body.limit ?? 100, body.operations])
}
