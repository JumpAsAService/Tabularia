// Pixel-perfect reports: a page designed in the browser, kept in a folder with its
// versions, rendered as PDF, Excel or HTML with its parameters (docs/specs/reports.md).
// The rules are the gateway's (routes/reports.py): VIEW opens and renders a report, EDIT
// changes it; what a render reads is checked with the reader's rights.
//
// Here the calls that read (lists, a report, its parameters, its versions, a render) and
// how a render is followed. Saving, the editor's preview, fields, duplicating and
// restoring are the designer's (useReportDesign.ts), which follows its preview with
// `followRender` from here.
import { FIRST_PAUSE_MS, nextPause } from '~/composables/waits'

// ── the definition (`v: 1`): lengths in millimetres, colours #rrggbb ─────────
export type ReportPageSize = 'A4' | 'A3' | 'A5' | 'Letter' | 'Legal' | { w: number; h: number }
export type ReportParamType = 'text' | 'number' | 'date' | 'boolean'

export interface ReportParameterDef {
  name: string
  label?: string
  type: ReportParamType
  multi?: boolean
  required?: boolean
  /** a value, a placeholder of Tabularia's language (`{today-30}`), or null */
  default?: unknown
  choices?: { values?: unknown[]; dataset?: string; value?: string; label?: string } | null
}

export interface ReportItem {
  id: string
  type: 'text' | 'image' | 'line' | 'rect' | 'table' | 'chart' | 'kpi'
  x: number
  y: number
  w: number
  h: number
  style?: Record<string, any>
  visible?: string | null
  [key: string]: any
}

export interface ReportBand {
  height?: number
  on_first_page?: boolean
  on_last_page?: boolean
  items: ReportItem[]
}

export interface ReportDefinition {
  v: 1
  page: { size: ReportPageSize; orientation: 'portrait' | 'landscape'; margins: [number, number, number, number] }
  locale: string
  timezone: string
  theme: { font: string; size: number; color: string; accent: string }
  parameters: ReportParameterDef[]
  datasets: { name: string; source: Record<string, any>; fields?: { name: string; type: string }[]; calculated?: { name: string; expr: string }[] }[]
  sections: { page_header: ReportBand; body: { items: ReportItem[] }; page_footer: ReportBand }
}

// ── the object ───────────────────────────────────────────────────────────────
export interface Report {
  id: number
  name: string
  description: string
  project_id: number
  current_version: number
  /** of the current version */
  datasets: number
  parameters: number
  created_at: string
  updated_at: string
  /** EDIT on its folder: changing, moving, deleting it, restoring a version */
  editable: boolean
}

export interface ReportDetail extends Report {
  content: ReportDefinition
}

export interface ReportVersion {
  version: number
  /** a code the interface translates: created, edited, restored from vN */
  note: string
  created_at: string
  created_by: string | null
  current: boolean
}

/** A parameter as the form shows it: the default resolved, the choices read. */
export interface ReportParameter {
  name: string
  label: string
  type: ReportParamType
  multi: boolean
  required: boolean
  /** resolved: a placeholder is its value today; a list for a multi one */
  default: unknown
  /** null: any value of its type */
  choices: { value: unknown; label: string | null }[] | null
  /** more than the list holds: the form takes any value */
  choices_truncated: boolean
  /** why the choices of a dataset could not be read */
  choices_error: string | null
}

/** A parameter's value as a render takes it: a text, a number, true/false, a list. */
export type ReportParamValue = string | number | boolean | null | (string | number | boolean)[]
export type ReportParams = Record<string, ReportParamValue>
/** A parameter's value in the form and in the address: a text, or texts; empty = none. */
export type ReportFormValue = string | string[]

export type ReportFormat = 'pdf' | 'xlsx' | 'html' | 'svg'

// ── the on-screen view: what a click changed, sent back with the next render ──
export interface ReportItemState {
  /** a column sorted on screen (its index), instead of the table's own order */
  sort?: { column: number; dir: 'asc' | 'desc' }
  /** the groups closed, each as its path of keys (as the hotspots give them) */
  collapsed?: unknown[][]
  /** with `collapse_all`: the groups open */
  expanded?: unknown[][]
  collapse_all?: boolean
}
export interface ReportState {
  items: Record<string, ReportItemState>
}

/** A box on a page, in millimetres from its top left corner. */
export interface PageBox { x: number; y: number; w: number; h: number }

/** Where a click on a page does something. `x`, `y`: millimetres from the page's top
 *  left corner — `x` where the cell's content starts (after its padding), `y` on the
 *  baseline of its text (measured on the pages: Typst places the mark there); `page` from 1.
 *  `box`: the clickable cell as the engine laid it out, in the same millimetres (renders
 *  from before it have none: see `placeHotspots`). */
export interface ReportHotspot {
  kind: 'sort' | 'toggle'
  item: string
  /** sort: the column's index */
  column?: number
  /** toggle: the group's path of keys (its level is the length minus one) */
  key?: unknown[]
  /** toggle: the group is closed now */
  closed?: boolean
  page: number
  x: number
  y: number
  box?: PageBox | null
}

/** A hotspot with the box it covers on its page (mm), and the column's header when the
 *  definition names it (the button's label). See `placeHotspots`. */
export interface PlacedHotspot extends ReportHotspot {
  box: PageBox
  header?: string
}

/** The pages on screen: one SVG per page, and where they can be clicked. */
export interface ReportScreen {
  pages: string[]
  hotspots: ReportHotspot[]
}

// ── following a render ───────────────────────────────────────────────────────
interface RenderStatus {
  status: 'running' | 'ready' | 'failed'
  filename?: string
  size?: number
  pages?: number
  warnings?: string[]
  code?: number
  detail?: string
}

export interface RenderOutcome {
  status: 'ready'
  task_id: string
  filename: string
  size: number | null
  /** pdf and svg: the number of pages */
  pages: number | null
  /** what the reader should know (a character its font lacks, replaced) */
  warnings: string[]
  /** the file's text (the svg format's JSON). The engine sends a file ONCE: call it once. */
  fileText: () => Promise<string>
  /** the browser saves the file, with its name. Once, as `fileText`. */
  download: (name?: string) => void
}

/** A render that stopped: `statusCode` as the engine tagged it (422 the definition, the
 *  data or a parameter; 404 a source that is gone; 503 the chart service; 504 too long). */
export interface RenderError extends Error {
  statusCode?: number
  data?: { detail?: unknown }
}

function aborted(): Error {
  return Object.assign(new Error('aborted'), { name: 'AbortError' })
}

function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(aborted())
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', stop)
      resolve()
    }, ms)
    const stop = () => {
      clearTimeout(timer)
      reject(aborted())
    }
    signal?.addEventListener('abort', stop, { once: true })
  })
}

export function useReports() {
  const { apiFetch } = useApiClient()
  const base = useRuntimeConfig().public.apiBase as string

  /**
   * Follows a render started by `render` (or the designer's preview) until its file is
   * written: often at first, then once a second (composables/waits.ts). Throws a
   * RenderError with the engine's reason when it fails, an AbortError when `signal` stops
   * it. A network blip does not lose a render still running.
   */
  async function followRender(taskId: string, opts: { signal?: AbortSignal } = {}): Promise<RenderOutcome> {
    const since = Date.now()
    let delay = FIRST_PAUSE_MS
    let failures = 0
    let state: RenderStatus
    for (;;) {
      await pause(delay, opts.signal)
      delay = nextPause(delay, since, 2000)
      try {
        state = await apiFetch<RenderStatus>(`/tasks/exports/${encodeURIComponent(taskId)}`, { signal: opts.signal })
        failures = 0
      } catch (e: any) {
        if (opts.signal?.aborted) throw aborted()
        const code = e?.response?.status ?? e?.statusCode
        if ((code && code < 500) || ++failures >= 5) throw e
        continue
      }
      if (state.status === 'failed') {
        throw Object.assign(new Error(state.detail ?? 'The render failed'), {
          statusCode: state.code, data: { detail: state.detail },
        }) as RenderError
      }
      if (state.status === 'ready') break
    }
    const fileUrl = `/tasks/exports/${encodeURIComponent(taskId)}/file`
    return {
      status: 'ready',
      task_id: taskId,
      filename: state.filename ?? '',
      size: state.size ?? null,
      pages: state.pages ?? null,
      warnings: state.warnings ?? [],
      fileText: () => apiFetch<string>(fileUrl, { responseType: 'text', signal: opts.signal }),
      download: (name?: string) => {
        const a = document.createElement('a')
        a.href = `${base}${fileUrl}`
        a.download = name ?? state.filename ?? ''
        a.click()
      },
    }
  }

  /** The pages on screen of a render in the svg format: its JSON read once. */
  async function screenOf(outcome: RenderOutcome): Promise<ReportScreen> {
    const parsed = JSON.parse(await outcome.fileText())
    return {
      pages: Array.isArray(parsed?.pages) ? parsed.pages.filter((p: unknown) => typeof p === 'string') : [],
      hotspots: Array.isArray(parsed?.hotspots) ? parsed.hotspots : [],
    }
  }

  return {
    list: () => apiFetch<Report[]>('/reports'),
    listByProject: (projectId: number) => apiFetch<Report[]>(`/projects/${projectId}/reports`),
    get: (id: number) => apiFetch<ReportDetail>(`/reports/${id}`),
    /** a new report in a folder (EDIT): with no content, the gateway's empty A4 page */
    create: (projectId: number, body: { name: string; description?: string; content?: ReportDefinition }) =>
      apiFetch<ReportDetail>(`/projects/${projectId}/reports`, { method: 'POST', body }),
    /** name, description, folder (a move): the Explore's changes. The definition is the designer's. */
    update: (id: number, body: { name?: string; description?: string; project_id?: number }) =>
      apiFetch<ReportDetail>(`/reports/${id}`, { method: 'PATCH', body }),
    duplicate: (id: number) => apiFetch<ReportDetail>(`/reports/${id}/duplicate`, { method: 'POST' }),
    remove: (id: number) => apiFetch<void>(`/reports/${id}`, { method: 'DELETE' }),
    versions: (id: number) => apiFetch<ReportVersion[]>(`/reports/${id}/versions`),
    version: (id: number, version: number) => apiFetch<ReportDefinition>(`/reports/${id}/versions/${version}`),
    restore: (id: number, version: number) =>
      apiFetch<ReportDetail>(`/reports/${id}/versions/${version}/restore`, { method: 'POST' }),
    /** the form: defaults resolved, choices read; `given` the values chosen so far (a
     *  choice may follow another parameter) */
    parameters: (id: number, given: ReportParams = {}) =>
      apiFetch<ReportParameter[]>(`/reports/${id}/parameters?${paramsQuery(given)}`),
    /** starts a render of the current version; followed with `followRender` */
    render: (id: number, body: { format: ReportFormat; params?: ReportParams; state?: ReportState | null }, signal?: AbortSignal) =>
      apiFetch<{ task_id: string; filename: string }>(`/reports/${id}/render`, { method: 'POST', body, signal }),
    followRender,
    screenOf,
    /** The address a script downloads the report from (GET, with an API token). */
    apiUrl: (id: number, format: Exclude<ReportFormat, 'svg'>, params: ReportParams = {}) => {
      const root = /^https?:\/\//.test(base) ? base : `${window.location.origin}${base}`
      const query = paramsQuery(params)
      return `${root.replace(/\/$/, '')}/reports/${id}/render?format=${format}${query ? `&${query}` : ''}`
    },
  }
}

/** The parameters with the choices of `fresh`, asked again with the values chosen so far
 *  (a choice may follow another parameter). Only the lists: `fresh` holds the values
 *  given as its «defaults», and the form keeps the report's own. */
export function withChoices(own: ReportParameter[], fresh: ReportParameter[]): ReportParameter[] {
  const byName = new Map(fresh.map((p) => [p.name, p]))
  return own.map((p) => {
    const f = byName.get(p.name)
    return f ? { ...p, choices: f.choices, choices_truncated: f.choices_truncated, choices_error: f.choices_error } : p
  })
}

/** The values a link or a script should carry: those that are not the parameter's
 *  default. One left out takes its default when the report is rendered, so a default such
 *  as «the last 30 days» keeps moving with the calendar instead of the day it was copied. */
export function explicitParams(parameters: Pick<ReportParameter, 'name' | 'default'>[], params: ReportParams): ReportParams {
  const texts = (v: unknown) => JSON.stringify((v === null || v === undefined ? [] : Array.isArray(v) ? v : [v]).map(String))
  const defaults = new Map(parameters.map((p) => [p.name, texts(p.default)]))
  return Object.fromEntries(Object.entries(params).filter(([name, v]) => defaults.get(name) !== texts(v)))
}

/** Parameters as a query string: a list repeats its name (`?region=a&region=b`), an
 *  empty value is left out (the report's default then holds). */
export function paramsQuery(params: ReportParams): string {
  const q = new URLSearchParams()
  for (const [name, value] of Object.entries(params)) {
    for (const v of Array.isArray(value) ? value : [value]) {
      if (v === null || v === undefined || v === '') continue
      q.append(name, String(v))
    }
  }
  return q.toString()
}

// ── where a hotspot is: its box on the page ──────────────────────────────────
// The engine gives the box of the clickable cell (`box`). Renders from before it gave only
// the point, and the box is rebuilt from the table's layout, as follows.
// The template's cells (backend/app/reports/template.typ): 1.5 mm of padding left and
// right, 1.1 mm above and below a header, 1.3 mm in a group's row, and 3 mm more to the
// left per level of group. Typst's line of text goes from the cap height to the baseline:
// 0.7 em in IBM Plex Sans. Measured on a rendered A4 (8 pt): a header from 126.4 to 130.5
// mm with its mark at 129.46, a group's row from 130.6 to 135.1 with its mark at 133.83.
const PAD_X = 1.5
const PAD_HEAD = 1.1
const PAD_GROUP = 1.3
const LEVEL_INDENT = 3
const CAP = 0.7
const PT_MM = 25.4 / 72
const PAGE_SIZES: Record<string, [number, number]> = {
  A4: [210, 297], A3: [297, 420], A5: [148, 210], Letter: [215.9, 279.4], Legal: [215.9, 355.6],
}

function findItem(definition: ReportDefinition | null | undefined, id: string): ReportItem | null {
  const s = definition?.sections
  if (!s) return null
  for (const items of [s.body?.items, s.page_header?.items, s.page_footer?.items]) {
    const hit = (items ?? []).find((i) => i.id === id)
    if (hit) return hit
  }
  return null
}

/** The engine's box, when it is one: four finite numbers, a width and a height. */
function engineBox(box: unknown): PageBox | null {
  if (!box || typeof box !== 'object') return null
  const { x, y, w, h } = box as Record<string, unknown>
  const finite = [x, y, w, h].every((n) => typeof n === 'number' && Number.isFinite(n))
  return finite && (w as number) > 0 && (h as number) > 0 ? { x: x as number, y: y as number, w: w as number, h: h as number } : null
}

/**
 * The boxes the hotspots cover, in mm on their page: the engine's `box` when it gives one.
 * Otherwise rebuilt: a header's box spans its column (to the next column's start, the
 * last one to the table's right edge); a group's row spans the table. The table's edges
 * and the columns' headers come from the definition the pages were rendered from;
 * without it, the boxes are a fair guess around the text.
 */
export function placeHotspots(hotspots: ReportHotspot[], definition?: ReportDefinition | null): PlacedHotspot[] {
  const line = (definition?.theme?.size ?? 9) * PT_MM * CAP
  const leftMargin = definition?.page?.margins?.[3] ?? null
  return hotspots.map((h) => {
    const item = findItem(definition, h.item)
    const header = h.kind === 'sort' && Array.isArray(item?.columns) && h.column != null ? item!.columns[h.column]?.header : undefined
    const named = typeof header === 'string' && header ? { header } : {}
    const measured = engineBox(h.box)
    if (measured) return { ...h, box: measured, ...named }
    const tableLeft = item && leftMargin != null ? leftMargin + item.x : null
    const tableRight = item && tableLeft != null ? tableLeft + item.w : null
    if (h.kind === 'toggle') {
      const level = Math.max(0, (h.key?.length ?? 1) - 1)
      const left = tableLeft ?? h.x - PAD_X - level * LEVEL_INDENT
      const right = tableRight ?? left + 80
      return { ...h, box: { x: left, y: h.y - line - PAD_GROUP, w: Math.max(4, right - left), h: line + 2 * PAD_GROUP } }
    }
    const row = hotspots.filter((o) => o.kind === 'sort' && o.item === h.item && o.page === h.page
      && Math.abs(o.y - h.y) < 0.3 && o.x > h.x)
    const next = row.length ? Math.min(...row.map((o) => o.x)) : null
    const left = h.x - PAD_X
    const right = next != null ? next - PAD_X : (tableRight ?? left + 25)
    return { ...h, box: { x: left, y: h.y - line - PAD_HEAD, w: Math.max(4, right - left), h: line + 2 * PAD_HEAD }, ...named }
  })
}

/** A page's size in mm, from its SVG (`viewBox` in points), or from the definition. */
export function pageSizeMm(svg: string | undefined, definition?: ReportDefinition | null): { w: number; h: number } {
  const m = svg?.slice(0, 600).match(/viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"/)
  if (m) return { w: Number(m[1]) * PT_MM, h: Number(m[2]) * PT_MM }
  const size = definition?.page?.size ?? 'A4'
  const [w, h] = typeof size === 'string' ? PAGE_SIZES[size] ?? PAGE_SIZES.A4 : [size.w, size.h]
  return definition?.page?.orientation === 'landscape' ? { w: Math.max(w, h), h: Math.min(w, h) } : { w, h }
}

/** The view after a click on a hotspot: a column sorted ascending, then descending, then
 *  back to the table's own order; a group closed or opened. Returns a new state. */
export function stateAfter(state: ReportState, h: ReportHotspot): ReportState {
  const items = { ...state.items }
  const cur: ReportItemState = { ...(items[h.item] ?? {}) }
  if (h.kind === 'sort' && h.column != null) {
    if (cur.sort?.column !== h.column) cur.sort = { column: h.column, dir: 'asc' }
    else if (cur.sort.dir === 'asc') cur.sort = { column: h.column, dir: 'desc' }
    else delete cur.sort
  } else if (h.kind === 'toggle' && h.key) {
    const same = (p: unknown[]) => JSON.stringify(p) === JSON.stringify(h.key)
    const without = (list?: unknown[][]) => (list ?? []).filter((p) => !same(p))
    if (cur.collapse_all) {
      cur.expanded = h.closed ? [...without(cur.expanded), h.key] : without(cur.expanded)
    } else {
      cur.collapsed = h.closed ? without(cur.collapsed) : [...without(cur.collapsed), h.key]
    }
    if (!cur.collapsed?.length) delete cur.collapsed
    if (!cur.expanded?.length) delete cur.expanded
  }
  if (Object.keys(cur).length) items[h.item] = cur
  else delete items[h.item]
  return { items }
}

/** Nothing changed on screen: the report as it is designed. */
export function isPlainState(state: ReportState | null | undefined): boolean {
  return !state || !Object.keys(state.items ?? {}).length
}

// «Page 1 of 3» in the report's language: the starter's footer is an expression
const PAGE_OF: Record<string, [string, string]> = {
  en: ['Page', 'of'], it: ['Pagina', 'di'], de: ['Seite', 'von'], es: ['Página', 'de'], fr: ['Page', 'sur'],
}

/** A new report's definition, as «New → Report» makes it: an A4 page standing up, the
 *  report's name as the header's title over a rule, «Page X of Y» in the footer, an
 *  empty body and no data yet. */
export function starterReport(locale: string, timezone: string): ReportDefinition {
  const lang = locale in PAGE_OF ? locale : 'en'
  const [page, of] = PAGE_OF[lang]
  return {
    v: 1,
    page: { size: 'A4', orientation: 'portrait', margins: [15, 12, 15, 12] },
    locale: lang,
    timezone: timezone || 'UTC',
    theme: { font: 'IBM Plex Sans', size: 9, color: '#1f2933', accent: '#3b5bdb' },
    parameters: [],
    datasets: [],
    sections: {
      page_header: {
        height: 18, on_first_page: true, items: [
          { id: 'title', type: 'text', x: 0, y: 2, w: 186, h: 9, value: '=REPORT()', style: { size: 16, weight: 'bold' } },
          { id: 'header_rule', type: 'line', x: 0, y: 14, w: 186, h: 0, style: { border: { width: 0.3, color: '#1f2933' } } },
        ],
      },
      body: { items: [] },
      page_footer: {
        height: 10, on_last_page: true, items: [
          { id: 'page_number', type: 'text', x: 106, y: 3, w: 80, h: 5,
            value: `="${page} " & PAGE() & " ${of} " & PAGES()`, style: { size: 8, align: 'right', color: '#52606d' } },
        ],
      },
    },
  }
}
