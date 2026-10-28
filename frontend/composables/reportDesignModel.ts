// The report designer's model: the definition (`v: 1`) as the designer edits it, typed
// item by item, and what the editor needs around it — the page's geometry in millimetres,
// the items a new drop makes, the cleaning before a save (lengths to a tenth, no empty
// expressions), and the problems of a refused save mapped to an item and a property.
// The rules are the gateway's (services/report_definition.py) and the engine's
// (backend/app/reports/definition.py); docs/specs/reports.md, «The definition».
import type { ReportDefinition } from '~/composables/useReports'

// ── the definition ───────────────────────────────────────────────────────────
export type BandName = 'page_header' | 'body' | 'page_footer'
export type ItemType = 'text' | 'image' | 'line' | 'rect' | 'table' | 'chart' | 'kpi'
export type Align = 'left' | 'center' | 'right'

export interface DStyle {
  size?: number | null // points
  weight?: 'normal' | 'semibold' | 'bold' | null
  italic?: boolean | null
  color?: string | null
  background?: string | null
  align?: Align | null
  valign?: 'top' | 'middle' | 'bottom' | null
  padding?: number | null
  border?: { width?: number; color?: string } | null
  radius?: number | null
  format?: string | null
  can_grow?: boolean
}

interface DBase {
  id: string
  x: number
  y: number
  w: number
  h: number
  style?: DStyle
  visible?: string | null
}

export interface DText extends DBase { type: 'text'; value: string; dataset?: string | null }
export interface DImage extends DBase { type: 'image'; asset: string; fit?: 'contain' | 'cover' | 'stretch' }
export interface DLine extends DBase { type: 'line' }
export interface DRect extends DBase { type: 'rect' }

export interface DRule { when: string; style: DStyle }
export interface DColumn {
  header?: string
  value: string
  width?: number | null
  align?: Align | null
  format?: string | null
  total?: string | null
  style?: DStyle
  rules?: DRule[]
}
export interface DGroup {
  by: string
  label?: string | null
  header?: boolean
  footer?: boolean
  page_break?: 'none' | 'between' | 'after'
  sort?: 'asc' | 'desc'
  keep_together?: boolean
}
export interface DMatrix { columns_by: string; value: string; total?: boolean; format?: string | null; width?: number | null }
export interface DTable extends DBase {
  type: 'table'
  dataset: string
  title?: string | null
  columns: DColumn[]
  groups?: DGroup[]
  detail?: boolean
  sort?: { by: string; dir?: 'asc' | 'desc' }[]
  filter?: string | null
  repeat_header?: boolean
  grand_total?: boolean
  zebra?: boolean
  matrix?: DMatrix | null
  interactive?: { sort?: boolean; toggle?: boolean }
}

export const CHART_KINDS = ['bar', 'column', 'line', 'area', 'pie', 'donut', 'combo', 'waterfall', 'funnel', 'treemap',
  'heatmap', 'scatter', 'gauge', 'bullet'] as const
export type ChartKind = typeof CHART_KINDS[number]
export interface DSeries { value: string; name?: string; as?: 'bar' | 'line' | null; axis?: 'left' | 'right' }
export interface DChartOptions {
  stacked?: boolean
  labels?: boolean
  legend?: boolean | null
  sort?: 'none' | 'asc' | 'desc'
  top?: number | null
  min?: number | null
  max?: number | null
  target?: number | string | null
  bands?: number[]
  format?: string | null
}
export interface DChart extends DBase {
  type: 'chart'
  dataset: string
  kind: ChartKind
  title?: string | null
  category?: string | null
  series: DSeries[]
  split_by?: string | null
  options?: DChartOptions
}
export interface DKpi extends DBase {
  type: 'kpi'
  dataset: string
  label?: string
  value: string
  compare?: string | null
  format?: string | null
  sparkline?: { category: string; value: string } | null
}

export type DItem = DText | DImage | DLine | DRect | DTable | DChart | DKpi

export interface DField { name: string; type: string }
export interface DCalculated { name: string; expr: string }
export interface DFilter { column: string; op: string; value?: unknown }
export type DSource =
  | { kind: 'sql'; sql: string; connection_id: number | null; engine: string | null }
  | { kind: 'datasource'; datasource_id: number; filters?: DFilter[] }
  | { kind: 'view'; view_id: number; filters?: DFilter[] }
  | { kind: 'sql_script'; script_id: number }
export interface DDataset { name: string; source: DSource; fields?: DField[]; calculated?: DCalculated[] }

export type ParamType = 'text' | 'number' | 'date' | 'boolean'
export interface DParameter {
  name: string
  label?: string
  type: ParamType
  multi?: boolean
  required?: boolean
  default?: unknown
  choices?: { values: unknown[] } | { dataset: string; value: string; label?: string | null } | null
}

export type PageSizeName = 'A4' | 'A3' | 'A5' | 'Letter' | 'Legal'
export interface DBand { height: number; on_first_page?: boolean; on_last_page?: boolean; items: DItem[] }
export interface DDefinition {
  v: 1
  page: { size: PageSizeName | { w: number; h: number }; orientation: 'portrait' | 'landscape'; margins: [number, number, number, number] }
  locale: string
  timezone: string
  theme: { font: string; size: number; color: string; accent: string }
  parameters: DParameter[]
  datasets: DDataset[]
  sections: { page_header: DBand; body: { items: DItem[] }; page_footer: DBand }
}

export const PAGE_SIZES: Record<PageSizeName, [number, number]> = {
  A4: [210, 297], A3: [297, 420], A5: [148, 210], Letter: [215.9, 279.4], Legal: [215.9, 355.6],
}
export const LOCALES = ['en', 'it', 'de', 'es', 'fr'] as const
export const PARAM_TYPES: ParamType[] = ['text', 'number', 'date', 'boolean']
// the placeholder language's names and the API's `format`: no parameter takes them
export const RESERVED_PARAMS = new Set(['today', 'yesterday', 'start_of_month', 'start_of_week', 'start_of_year', 'now',
  'last_refresh', 'run_id', 'flow', 'date', 'datetime', 'format'])
export const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
export const ITEM_ID_RE = /^[A-Za-z0-9_-]{1,64}$/
/** What a page header or footer holds. */
export const BAND_ITEM_TYPES: ItemType[] = ['text', 'image', 'line', 'rect']
export const FILTER_OPS = ['=', '<>', '<', '<=', '>', '>=', 'in', 'contains'] as const
export const MAX_IMAGE_BYTES = 512 * 1024

// The printed page's own colours (backend/app/reports/model.py, PALETTE): the sheet is
// paper whatever the interface's theme, so the design view draws with these.
export const PAPER = {
  sheet: '#ffffff', rule: '#d9dde5', band: '#e9eef9', head: '#f1f3f5', total: '#1f2933', muted: '#6b7590',
}

// ── geometry (mm) ────────────────────────────────────────────────────────────
export const PT_MM = 25.4 / 72
export const mm1 = (v: number) => Math.round(v * 10) / 10

export function pageSize(def: DDefinition): { w: number; h: number } {
  const s = def.page.size
  const [w, h] = typeof s === 'string' ? PAGE_SIZES[s] ?? PAGE_SIZES.A4 : [s.w, s.h]
  return def.page.orientation === 'landscape' ? { w: Math.max(w, h), h: Math.min(w, h) } : { w, h }
}

/** The width between the margins: every item's x runs from 0 to it. */
export function contentWidth(def: DDefinition): number {
  const [, right, , left] = def.page.margins
  return mm1(pageSize(def).w - left - right)
}

/** The body's height on one page: what is left between the header and the footer. */
export function bodyPageHeight(def: DDefinition): number {
  const [top, , bottom] = def.page.margins
  return mm1(pageSize(def).h - top - bottom - def.sections.page_header.height - def.sections.page_footer.height)
}

export function itemsOf(def: DDefinition, band: BandName): DItem[] {
  return def.sections[band].items
}

export function allowedIn(band: BandName, type: ItemType): boolean {
  return band === 'body' || BAND_ITEM_TYPES.includes(type)
}

/** An item's box as drawn: a line's from its two ends. */
export function boxOf(it: DItem): { x: number; y: number; w: number; h: number } {
  if (it.type === 'line') {
    return { x: Math.min(it.x, it.x + it.w), y: Math.min(it.y, it.y + it.h), w: Math.abs(it.w), h: Math.abs(it.h) }
  }
  return { x: it.x, y: it.y, w: it.w, h: it.h }
}

/** A table, matrix or list takes its row of the body alone: the items whose vertical
 *  range overlaps it (the gateway's _alone_in_its_row). */
export function rowConflicts(items: DItem[], one?: DItem): { table: DItem; other: DItem }[] {
  const out: { table: DItem; other: DItem }[] = []
  for (const t of items) {
    if (t.type !== 'table') continue
    const top = t.y
    const bottom = t.y + t.h
    for (const other of items) {
      if (other === t) continue
      if (one && other !== one && t !== one) continue
      const b = boxOf(other)
      if (b.y < bottom - 1e-6 && b.y + Math.max(b.h, 0.0001) > top + 1e-6) out.push({ table: t, other })
    }
  }
  return out
}

// ── the parts of a table as the design view draws them ───────────────────────
export type TableRowKind = 'title' | 'head' | 'group-head' | 'detail' | 'group-foot' | 'total'
export interface TableRow { kind: TableRowKind; level: number }

/** The rows a table shows in the design view, in order: its header, every group's header,
 *  the detail, every group's footer from the innermost out, the grand total. */
export function tableRows(t: DTable): TableRow[] {
  const rows: TableRow[] = [{ kind: 'head', level: 0 }]
  // a matrix's innermost group is its rows (the cells): only the outer ones have a header and a total
  const groups = t.matrix ? (t.groups ?? []).slice(0, -1) : t.groups ?? []
  groups.forEach((g, i) => { if (g.header !== false) rows.push({ kind: 'group-head', level: i }) })
  if (t.matrix || t.detail !== false || !groups.length) rows.push({ kind: 'detail', level: groups.length })
  for (let i = groups.length - 1; i >= 0; i--) if (groups[i].footer !== false) rows.push({ kind: 'group-foot', level: i })
  if (t.grand_total) rows.push({ kind: 'total', level: 0 })
  return rows
}

/** A row's height in the design view, from the theme's size (the template's cells: 1.1 mm
 *  above and below a line of text). */
export function rowHeight(def: DDefinition): number {
  return mm1(2.6 + def.theme.size * PT_MM * 1.25)
}

export function tableHeight(def: DDefinition, t: DTable): number {
  return mm1(tableRows(t).length * rowHeight(def))
}

// ── new things ───────────────────────────────────────────────────────────────
export function emptyDefinition(): DDefinition {
  return {
    v: 1,
    page: { size: 'A4', orientation: 'portrait', margins: [15, 12, 15, 12] },
    locale: 'en',
    timezone: 'UTC',
    theme: { font: 'IBM Plex Sans', size: 9, color: '#1f2933', accent: '#3b5bdb' },
    parameters: [],
    datasets: [],
    sections: {
      page_header: { height: 18, on_first_page: true, items: [] },
      body: { items: [] },
      page_footer: { height: 10, on_last_page: true, items: [] },
    },
  }
}

/** A definition from the server made whole: what a part leaves out takes its default. */
export function normalize(raw: ReportDefinition | Record<string, any> | null | undefined): DDefinition {
  const base = emptyDefinition()
  const r = (raw ?? {}) as Record<string, any>
  const s = r.sections ?? {}
  const band = (b: any, key: 'on_first_page' | 'on_last_page', fallback: DBand): DBand => ({
    height: typeof b?.height === 'number' ? b.height : fallback.height,
    [key]: b?.[key] !== false,
    items: Array.isArray(b?.items) ? b.items : [],
  })
  const margins = Array.isArray(r.page?.margins) && r.page.margins.length === 4 ? r.page.margins : base.page.margins
  return {
    v: 1,
    page: {
      size: r.page?.size ?? 'A4',
      orientation: r.page?.orientation === 'landscape' ? 'landscape' : 'portrait',
      margins: margins.map((m: unknown) => (typeof m === 'number' ? m : 0)) as DDefinition['page']['margins'],
    },
    locale: typeof r.locale === 'string' ? r.locale : 'en',
    timezone: typeof r.timezone === 'string' && r.timezone ? r.timezone : 'UTC',
    theme: { ...base.theme, ...(r.theme ?? {}) },
    parameters: Array.isArray(r.parameters) ? r.parameters : [],
    datasets: (Array.isArray(r.datasets) ? r.datasets : []).map((d: any) => ({
      ...d, fields: Array.isArray(d.fields) ? d.fields : [], calculated: Array.isArray(d.calculated) ? d.calculated : [],
    })),
    sections: {
      page_header: band(s.page_header, 'on_first_page', base.sections.page_header),
      body: { items: Array.isArray(s.body?.items) ? s.body.items : [] },
      page_footer: band(s.page_footer, 'on_last_page', base.sections.page_footer),
    },
  }
}

const PREFIX: Record<ItemType, string> = {
  text: 'text', image: 'image', line: 'line', rect: 'rect', table: 'table', chart: 'chart', kpi: 'kpi',
}

/** A free id for a new item: its type and the first number not taken. */
export function freeId(def: DDefinition, type: ItemType | 'matrix'): string {
  const taken = new Set<string>()
  for (const b of ['page_header', 'body', 'page_footer'] as BandName[]) for (const it of def.sections[b].items) taken.add(it.id)
  const prefix = type === 'matrix' ? 'matrix' : PREFIX[type]
  for (let i = 1; ; i++) if (!taken.has(`${prefix}${i}`)) return `${prefix}${i}`
}

export type InsertKind = ItemType | 'matrix'

/** A new item of `kind`, at its default size; a table, matrix and chart on the first
 *  dataset, which the person changes in the properties. */
export function newItem(def: DDefinition, kind: InsertKind, at: { x: number; y: number }, dataset?: string): DItem {
  const id = freeId(def, kind)
  const ds = dataset ?? def.datasets[0]?.name ?? ''
  const width = contentWidth(def)
  const base = { id, x: mm1(at.x), y: mm1(at.y) }
  switch (kind) {
    case 'text':
      return { ...base, type: 'text', w: 60, h: 7, value: '' }
    case 'image':
      return { ...base, type: 'image', w: 30, h: 15, asset: '', fit: 'contain' }
    case 'line':
      return { ...base, type: 'line', w: 60, h: 0, style: { border: { width: 0.3, color: '#1f2933' } } }
    case 'rect':
      return { ...base, type: 'rect', w: 40, h: 20, style: { border: { width: 0.3, color: '#d9dde5' }, radius: 1.5 } }
    case 'table': {
      const t: DTable = { ...base, type: 'table', x: 0, w: width, h: 10, dataset: ds, columns: [], groups: [],
        detail: true, repeat_header: true, zebra: false, grand_total: false }
      t.h = tableHeight(def, t)
      return t
    }
    case 'matrix': {
      const t: DTable = { ...base, type: 'table', x: 0, w: width, h: 10, dataset: ds,
        columns: [{ header: '', value: '' }], groups: [{ by: '', header: false, footer: false }], detail: false, repeat_header: true,
        matrix: { columns_by: '', value: '', total: true } }
      t.h = tableHeight(def, t)
      return t
    }
    case 'chart':
      return { ...base, type: 'chart', w: Math.min(120, width), h: 60, dataset: ds, kind: 'column', category: '',
        series: [{ value: '' }], options: {} }
    case 'kpi':
      return { ...base, type: 'kpi', w: 58, h: 24, dataset: ds, label: '', value: '' }
  }
}

/** A copy of an item with a new id (duplicate, paste). */
export function copyItem(def: DDefinition, it: DItem, dx = 5, dy = 5): DItem {
  const copy = JSON.parse(JSON.stringify(it)) as DItem
  copy.id = freeId(def, it.type === 'table' && (it as DTable).matrix ? 'matrix' : it.type)
  copy.x = mm1(it.x + dx)
  copy.y = mm1(it.y + dy)
  return copy
}

// ── before a save ────────────────────────────────────────────────────────────
const EXPRESSION_KEYS = new Set(['visible', 'filter', 'total', 'label', 'category', 'split_by', 'compare', 'format', 'title'])

function prune(v: any, key = ''): any {
  if (Array.isArray(v)) return v.map((x) => prune(x))
  if (v && typeof v === 'object') {
    const out: Record<string, any> = {}
    for (const [k, x] of Object.entries(v)) {
      if (x === undefined || x === null) continue
      // an optional expression or text left empty is none at all (an empty expression is a mistake)
      if (x === '' && (EXPRESSION_KEYS.has(k) || ['color', 'background', 'header', 'dataset'].includes(k))) continue
      const p = prune(x, k)
      if (k === 'style' && p && typeof p === 'object' && !Object.keys(p).length) continue
      out[k] = p
    }
    return out
  }
  if (typeof v === 'number' && ['x', 'y', 'w', 'h', 'width', 'height', 'padding', 'radius'].includes(key)) return mm1(v)
  return v
}

/** The definition as it is saved: lengths in tenths of mm, no empty optional values. The
 *  parameters' and datasets' own shapes are kept (their dialogs build them clean). */
export function forSave(def: DDefinition): DDefinition {
  const copy = JSON.parse(JSON.stringify(def)) as DDefinition
  copy.page.margins = copy.page.margins.map(mm1) as DDefinition['page']['margins']
  copy.sections.page_header.height = mm1(copy.sections.page_header.height)
  copy.sections.page_footer.height = mm1(copy.sections.page_footer.height)
  for (const b of ['page_header', 'body', 'page_footer'] as BandName[]) {
    copy.sections[b].items = copy.sections[b].items.map((it) => {
      const clean = prune(it) as DItem
      if (clean.type === 'chart' && !clean.options) clean.options = {}
      if (clean.type === 'table' && clean.matrix === undefined) delete clean.matrix
      // a matrix's first column is its rows' labels: the engine prints the innermost group
      // there, the gateway wants a value all the same
      if (clean.type === 'table' && clean.matrix && clean.columns[0] && !clean.columns[0].value?.trim()) {
        clean.columns[0].value = clean.groups?.[clean.groups.length - 1]?.by || '""'
      }
      return clean
    })
  }
  return copy
}

// a 1×1 PNG: what an image becomes in a definition sent to be checked
const CHECK_IMAGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='

/** The definition as the live check reads it: as it would be saved, its images (up to half
 *  a megabyte each, and checked at the save) replaced by a pixel, so a pause in the typing
 *  does not send them every time. */
export function forCheck(def: DDefinition): DDefinition {
  const copy = forSave(def)
  for (const b of ['page_header', 'body', 'page_footer'] as BandName[]) {
    for (const it of copy.sections[b].items) if (it.type === 'image' && it.asset) it.asset = CHECK_IMAGE
  }
  return copy
}

// ── the problems of a refused save ───────────────────────────────────────────
/** A mistake mapped to where the designer shows it: an item (its id) and the property
 *  inside it (`columns[0].value`), a dataset, a parameter, or the report itself. */
export interface DesignProblem {
  message: string
  item: string | null
  dataset: string | null
  parameter: string | null
  /** the property, relative to the item, dataset or parameter: `columns[0].value`, `source.sql` */
  path: string
  /** inside an expression: the first character (0-based) and how many */
  pos: number | null
  length: number
  /** what the property held when the server judged it: once it changes, the problem goes */
  seen?: string
}

function datasetName(def: DDefinition, key: string): string | null {
  if (/^\d+$/.test(key)) return def.datasets[Number(key)]?.name ?? null
  return key
}
function parameterName(def: DDefinition, key: string): string | null {
  if (/^\d+$/.test(key)) return def.parameters[Number(key)]?.name ?? null
  return key
}

function one(def: DDefinition, raw: any): DesignProblem {
  const message = String(raw?.message ?? '')
  let item: string | null = typeof raw?.item === 'string' ? raw.item : null
  let where = String(raw?.where ?? raw?.field ?? '')
  let dataset: string | null = null
  let parameter: string | null = null
  // the gateway's paths start at the definition's root: sections.<band>.items[i].<rest>
  const inItem = /^sections\.(page_header|body|page_footer)\.items(?:\[(\d+)\])?\.?(.*)$/.exec(where)
  if (inItem) {
    const [, band, index, rest] = inItem
    if (!item && index != null) item = def.sections[band as BandName].items[Number(index)]?.id ?? null
    where = index != null ? rest : ''
  }
  const inDataset = /^datasets\[([^\]]+)\]\.?(.*)$/.exec(where)
  if (inDataset) {
    dataset = datasetName(def, inDataset[1])
    where = inDataset[2]
  }
  const inParameter = /^parameters\[([^\]]+)\]\.?(.*)$/.exec(where)
  if (inParameter) {
    parameter = parameterName(def, inParameter[1])
    where = inParameter[2]
  }
  // the engine names a matrix's one level `columns_by[0]`, the gateway `columns_by`
  where = where.replace(/^matrix\.columns_by\[0\]$/, 'matrix.columns_by')
  return {
    message, item, dataset, parameter, path: where,
    pos: typeof raw?.pos === 'number' ? raw.pos : null,
    length: typeof raw?.length === 'number' ? raw.length : 0,
  }
}

/** The problems of a 422 or of the live check: the list (`problems` of the check and of a
 *  refused preview, `errors` of a refused save), or the gateway's one problem. */
export function problemsOf(def: DDefinition, detail: unknown): DesignProblem[] {
  if (!detail || typeof detail !== 'object') {
    return typeof detail === 'string' && detail
      ? [{ message: detail, item: null, dataset: null, parameter: null, path: '', pos: null, length: 0 }]
      : []
  }
  const d = detail as Record<string, any>
  // the check's answer with no problems is an empty list; a refusal always has its message
  const list = Array.isArray(d.problems) && (d.problems.length || !('message' in d)) ? d.problems
    : Array.isArray(d.errors) && d.errors.length ? d.errors : [d]
  return list.filter((x: any) => x && typeof x === 'object').map((x: any) => one(def, x))
}

/** A problem the engine wrote as text (a render refused: «the report's definition: item
 *  «k1», value: there is no field [x] (at character 5)»), mapped as problemsOf does; null
 *  when the text names no item. */
export function problemFromText(text: string): DesignProblem | null {
  const m = /item «([^»]+)», ([^:]+): (.*?)(?: \(at character (\d+)\))?$/s.exec(text)
  if (!m) return null
  return { message: m[3], item: m[1], dataset: null, parameter: null, path: m[2].trim(),
           pos: m[4] ? Number(m[4]) - 1 : null, length: 0 }
}

/** The value at a property path of an item, a dataset or a parameter (`columns[0].value`). */
export function valueAt(root: unknown, path: string): unknown {
  let cur: any = root
  for (const part of path.match(/[^.[\]]+/g) ?? []) {
    if (cur == null) return undefined
    cur = cur[/^\d+$/.test(part) ? Number(part) : part]
  }
  return cur
}

/** Where a problem points, in the definition being edited. */
export function problemRoot(def: DDefinition, p: DesignProblem): unknown {
  if (p.item) {
    for (const b of ['page_header', 'body', 'page_footer'] as BandName[]) {
      const hit = def.sections[b].items.find((i) => i.id === p.item)
      if (hit) return hit
    }
    return undefined
  }
  if (p.dataset) return def.datasets.find((d) => d.name === p.dataset)
  if (p.parameter) return def.parameters.find((x) => x.name === p.parameter)
  return def
}

/** The problems with what their property held, to forget them when it changes. */
export function withSeen(def: DDefinition, list: DesignProblem[]): DesignProblem[] {
  return list.map((p) => ({ ...p, seen: JSON.stringify(valueAt(problemRoot(def, p), p.path) ?? null) }))
}

/** The problems still standing: their item is there and their property unchanged (an
 *  undo, a paste, a rename by hand). */
export function stillStanding(def: DDefinition, list: DesignProblem[]): DesignProblem[] {
  return list.filter((p) => {
    const root = problemRoot(def, p)
    if (root === undefined) return false
    return p.seen === undefined || !p.path || JSON.stringify(valueAt(root, p.path) ?? null) === p.seen
  })
}

/** A problem of the live check, shown at its field: only while the field holds what the
 *  server judged (a change hides it at once, before the next answer), and not on an empty
 *  field, which is being written (the save says what is missing). */
export function liveShown(def: DDefinition, p: DesignProblem): boolean {
  const root = problemRoot(def, p)
  if (root === undefined || !p.path) return false
  const now = valueAt(root, p.path)
  if (now == null || (typeof now === 'string' && !now.trim())) return false
  return p.seen === undefined || JSON.stringify(now) === p.seen
}

/** A path written by the editor (`columns.0.value`) as the problems write it (`columns[0].value`). */
export function samePath(a: string, b: string): boolean {
  const norm = (p: string) => p.replace(/\.(\d+)(?=\.|$)/g, '[$1]').replace(/^\./, '')
  return norm(a) === norm(b)
}

// ── fields ───────────────────────────────────────────────────────────────────
export type FieldKind = 'text' | 'number' | 'date' | 'boolean' | 'other'

/** A field's kind from its type (the engine's field_type, in short). */
export function fieldKind(type: string | undefined): FieldKind {
  const d = (type ?? '').toLowerCase()
  if (/^(int|uint|float|double|decimal|number|numeric|real|bigint)/.test(d)) return 'number'
  if (/^(str|text|utf8|varchar|char|categorical|enum)/.test(d)) return 'text'
  if (/^(date|datetime|timestamp)/.test(d)) return 'date'
  if (d.startsWith('bool')) return 'boolean'
  return 'other'
}

/** An expression's field, written as the language writes it: `]` doubled. */
export function fieldRef(name: string): string {
  return `[${name.replaceAll(']', ']]')}]`
}

/** The default expressions for a column made from a dropped field. */
export function columnFor(field: DField): DColumn {
  const kind = fieldKind(field.type)
  // a whole number may be a code or a period (202401): right-aligned, but no format and no
  // total until the person asks for them; an amount gets both
  const amount = kind === 'number' && !/^(int|uint|bigint|integer)/i.test(field.type)
  return {
    header: field.name,
    value: fieldRef(field.name),
    ...(kind === 'number' ? { align: 'right' as Align } : {}),
    ...(amount ? { format: '#,##0.00', total: `SUM(${fieldRef(field.name)})` } : {}),
  }
}
