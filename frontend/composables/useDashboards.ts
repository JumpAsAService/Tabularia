// Dashboards: a screen of live tiles on a grid, kept in a folder with its versions
// (docs/specs/dashboards.md). The rules are the gateway's (routes/dashboards.py): VIEW opens a
// dashboard, EDIT changes it; what a tile reads is checked with the reader's rights.
// a type only: useApi.ts reaches Nuxt at import time, and the tests load this module bare
import type { Page } from '~/composables/useApi'
import {
  NO_VALUE, paramsOfForm, paramsQuery, type ReportFormValue, type ReportParameter, type ReportParams, type ReportState,
} from '~/composables/useReports'

export interface DashboardPage { size?: string | { w: number; h: number }; orientation?: 'portrait' | 'landscape'; margins?: number[] }

export interface DashboardDefinition {
  v: 1
  locale: string
  timezone: string
  refresh_seconds: number // 0: by hand only; otherwise at least 60
  page?: DashboardPage // of its PDF and its image: A4 across unless one is chosen
  parameters: Record<string, any>[]
  datasets: Record<string, any>[]
  tiles: Record<string, any>[]
}

export interface Dashboard {
  id: number
  name: string
  description: string
  project_id: number
  current_version: number
  datasets: number
  parameters: number
  tiles: number
  created_at: string
  updated_at: string
  editable: boolean
}

export interface DashboardDetail extends Dashboard {
  content: DashboardDefinition
}

/** A row of the Dashboards tab (GET /dashboards/search): where it lives, who made it, who reads it. */
export interface DashboardRow extends Dashboard {
  folder_path: string[]
  owner_name: string | null
  /** opened in the viewer in the last 30 days: by how many people, how many times (a person once a day) */
  people_30d: number
  opens_30d: number
  views_total: number
  last_viewed_at: string | null
}
export type DashboardSort = 'trending' | 'name' | 'updated'

// ── the answer, tile by tile (docs/specs/dashboards.md) ──────────────────────
export interface TileError { error: string; code: string }
export interface TileNumbers {
  type: string
  title: string
  computed_at: string
  cached: boolean
  hidden?: boolean
  [key: string]: any
}
export type TileAnswer = TileError | TileNumbers
export interface TilesAnswer { tiles: Record<string, TileAnswer>; truncated: Record<string, boolean> }

/** A document of the dashboard, with what is on screen: its parameters, its clicks, its view. */
export interface DashboardRenderRequest {
  format: 'pdf' | 'png'
  params: ReportParams
  clicks?: { tile: string; on?: string; values: unknown[] }[]
  state?: ReportState | null
}

export interface TilesRequest {
  params: ReportParams
  tiles?: string[]
  page?: Record<string, number>
  state?: ReportState | null
  clicks?: { tile: string; on?: string; values: unknown[] }[]
  refresh?: boolean
}

/** A new dashboard: no data yet, refreshed by hand, in the language of whoever makes it. */
export function starterDashboard(locale: string, timezone: string): DashboardDefinition {
  return {
    v: 1,
    locale: ['en', 'it', 'de', 'es', 'fr'].includes(locale) ? locale : 'en',
    timezone: timezone || 'UTC',
    refresh_seconds: 0,
    parameters: [],
    datasets: [],
    tiles: [],
  }
}

// ── the parameters in the address, as a report's (pages/report.vue) ──────────
// A parameter called `id` would meet the dashboard's own: in the address it is `_id` (a
// parameter's name starts with a letter, so `_id` is nobody's).
const urlKey = (name: string) => (name === 'id' ? '_id' : name)
const freeMulti = (p: ReportParameter) => p.multi && !(p.choices && !p.choices_truncated)
const sameValue = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

function defaultValue(p: ReportParameter): ReportFormValue {
  const d = p.default
  const texts = (d === null || d === undefined ? [] : Array.isArray(d) ? d : [d]).map((v) => String(v))
  if (!p.multi) return texts[0] ?? ''
  return freeMulti(p) ? texts.join(', ') : texts
}

/** The values an address holds, by the name of their parameter; a list repeats its name. */
export function valuesInQuery(query: Record<string, unknown>): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const [k, v] of Object.entries(query)) {
    if (k === 'id') continue
    out[k === '_id' ? 'id' : k] = (Array.isArray(v) ? v : [v]).filter((x): x is string => typeof x === 'string')
  }
  return out
}

/** The form for those values: the parameter's default where the address says nothing, and an empty
 *  field for `{none}`. */
export function formOfQuery(parameters: ReportParameter[], given: Record<string, string[]>): Record<string, ReportFormValue> {
  const form: Record<string, ReportFormValue> = {}
  for (const p of parameters) {
    const v = given[p.name]
    if (!v || !v.length) form[p.name] = defaultValue(p)
    else if (v.length === 1 && v[0] === NO_VALUE) form[p.name] = freeMulti(p) || !p.multi ? '' : []
    else if (!p.multi) form[p.name] = v[0]
    else form[p.name] = freeMulti(p) ? v.join(', ') : v
  }
  return form
}

/** The address of these values: the dashboard, and the values that differ from their defaults, so
 *  that a default such as «the last 30 days» keeps moving with the calendar. */
export function dashboardQuery(id: number, parameters: ReportParameter[], values: Record<string, ReportFormValue>): Record<string, string | string[]> {
  const q: Record<string, string | string[]> = { id: String(id) }
  const params = paramsOfForm(parameters, values)
  for (const p of parameters) {
    const v = params[p.name]
    if (v === undefined || v === null || sameValue(values[p.name], defaultValue(p))) continue
    q[urlKey(p.name)] = Array.isArray(v) ? v.map(String) : String(v)
  }
  return q
}

export function useDashboards() {
  const { apiFetch } = useApiClient()

  return {
    /** every dashboard this person sees: a flow group's Pushmail cell can attach them */
    list: () => apiFetch<Dashboard[]>('/dashboards'),
    /** the Dashboards tab: paginated, searched on the server over name, description and folders */
    listPaged: (p: { q?: string; limit: number; offset: number; sort: DashboardSort }) => {
      const query = new URLSearchParams({ limit: String(p.limit), offset: String(p.offset), sort: p.sort })
      if (p.q) query.set('q', p.q)
      return apiFetch<Page<DashboardRow>>(`/dashboards/search?${query}`)
    },
    listByProject: (projectId: number) => apiFetch<Dashboard[]>(`/projects/${projectId}/dashboards`),
    get: (id: number) => apiFetch<DashboardDetail>(`/dashboards/${id}`),
    /** the viewer has it on screen: in the audit, and in the count of who reads it */
    opened: (id: number) => apiFetch<void>(`/dashboards/${id}/views`, { method: 'POST' }),
    /** the form: defaults resolved, choices read; `given` the values chosen so far (a choice may
     *  follow another parameter) */
    parameters: (id: number, given: ReportParams = {}) =>
      apiFetch<ReportParameter[]>(`/dashboards/${id}/parameters?${paramsQuery(given)}`),
    /** the numbers ready to draw, tile by tile; an error of a tile stays in that tile */
    data: (id: number, body: TilesRequest, signal?: AbortSignal) =>
      apiFetch<TilesAnswer>(`/dashboards/${id}/data`, { method: 'POST', body, signal }),
    /** starts a document: followed and taken as a report's (useReports().followRender) */
    render: (id: number, body: DashboardRenderRequest) =>
      apiFetch<{ task_id: string; filename: string }>(`/dashboards/${id}/render`, { method: 'POST', body }),
    /** the folders where this person can make one: EDIT */
    folders: () => apiFetch<number[]>('/dashboards/folders'),
    create: (projectId: number, body: { name: string; description?: string; content?: DashboardDefinition }) =>
      apiFetch<DashboardDetail>(`/projects/${projectId}/dashboards`, { method: 'POST', body }),
    update: (id: number, body: { name?: string; description?: string; project_id?: number; content?: DashboardDefinition; base_version?: number }) =>
      apiFetch<DashboardDetail>(`/dashboards/${id}`, { method: 'PATCH', body }),
    // ── the designer, on a definition not saved yet: EDIT on the folder ──
    /** every problem of the definition, as a save would say them */
    check: (id: number, definition: unknown, signal?: AbortSignal) =>
      apiFetch<{ problems: unknown[] }>('/dashboards/check', { method: 'POST', body: { dashboard_id: id, definition }, signal }),
    /** the numbers of a definition being edited; a 422 lists its problems. `tab`: a newer request knocks
     *  down an older one of this tab only */
    preview: (id: number, definition: DashboardDefinition, body: TilesRequest, tab: string, signal?: AbortSignal) =>
      apiFetch<TilesAnswer>('/dashboards/preview', { method: 'POST', body: { dashboard_id: id, definition, tab, ...body }, signal }),
    parametersOf: (id: number, definition: DashboardDefinition, params: ReportParams, signal?: AbortSignal) =>
      apiFetch<ReportParameter[]>(`/dashboards/${id}/parameters`, { method: 'POST', body: { definition, params }, signal }),
    /** a dataset's columns as the data has them, and its rows counted when they can be */
    fields: (id: number, dataset: string, definition: unknown) =>
      apiFetch<{ dataset: string; fields: { name: string; type: string }[]; rows?: number; over?: boolean; max_rows?: number }>(`/dashboards/${id}/fields`, {
        method: 'POST', body: { dataset, definition },
      }),
    duplicate: (id: number) => apiFetch<DashboardDetail>(`/dashboards/${id}/duplicate`, { method: 'POST' }),
    remove: (id: number) => apiFetch<void>(`/dashboards/${id}`, { method: 'DELETE' }),
  }
}
