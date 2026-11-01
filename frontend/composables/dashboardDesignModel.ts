// A dashboard in the designer. The panels of the report designer (components/report/designer) edit
// a report's definition through its store, with its history and its problems: so a dashboard goes in
// written as a report's, as the gateway writes it to check it (services/dashboards.py, `as_report`),
// each tile an item of the body with its `grid`, and comes out as it was (`definitionOf`). Pure
// functions, tested without a browser (tests/dashboards.test.mjs).
import {
  columnFor, emptyDefinition, fieldKind, fieldRef, forCheck, forSave, problemsOf, type DDefinition, type DField, type DItem, type DesignProblem,
} from '~/composables/reportDesignModel'
import { freeSpot, type GridBox } from '~/composables/dashboardGrid'
// a type only: useDashboards.ts reaches the API
import type { DashboardDefinition } from '~/composables/useDashboards'

export type DBoard = DDefinition & { refresh_seconds: number }
export type DTile = Record<string, any> & { id: string; type: string; grid: GridBox }
export type TileKind = 'kpi' | 'chart' | 'table' | 'matrix' | 'text' | 'image'

export const REFRESH_LEAST = 60
export const REFRESH_MOST = 24 * 3600

export const tilesOf = (doc: DDefinition) => doc.sections.body.items as unknown as DTile[]

/** A dashboard written as the report designer's store holds a report. */
export function boardOf(d: DashboardDefinition | Record<string, any>): DBoard {
  const base = emptyDefinition()
  return {
    ...base,
    locale: d.locale || 'en',
    timezone: d.timezone || 'UTC',
    refresh_seconds: typeof d.refresh_seconds === 'number' ? d.refresh_seconds : 0,
    parameters: (d.parameters ?? []) as DDefinition['parameters'],
    datasets: (d.datasets ?? []).map((x: any) => ({ ...x, fields: x.fields ?? [], calculated: x.calculated ?? [] })),
    sections: {
      page_header: { height: 0, on_first_page: true, items: [] },
      body: { items: (d.tiles ?? []) as unknown as DItem[] },
      page_footer: { height: 0, on_last_page: true, items: [] },
    },
  }
}

function asDashboard(saved: DDefinition, doc: DBoard): DashboardDefinition {
  return {
    v: 1,
    locale: saved.locale,
    timezone: saved.timezone,
    refresh_seconds: doc.refresh_seconds,
    parameters: saved.parameters as unknown as Record<string, any>[],
    datasets: saved.datasets as unknown as Record<string, any>[],
    // what the store writes for a report's table is not a tile's: a tile has its place in `grid`
    tiles: saved.sections.body.items.map((it) => {
      const { x: _x, y: _y, w: _w, h: _h, ...tile } = it as unknown as Record<string, any>
      return tile
    }),
  }
}

/** The definition as it is saved. */
export const definitionOf = (doc: DBoard): DashboardDefinition => asDashboard(forSave(doc), doc)

/** As `definitionOf`, with a pixel in place of each image: what goes at every pause in the editing. */
export const draftOf = (doc: DBoard): DashboardDefinition => asDashboard(forCheck(doc), doc)

/** A definition without some of its tiles, which would stop the whole request. */
export const withoutTiles = (d: DashboardDefinition, out: Set<string>): DashboardDefinition =>
  (out.size ? { ...d, tiles: d.tiles.filter((t) => !out.has(t.id)) } : d)

/** A layout worked out by the grid (dashboardGrid.ts), written on the tiles that moved. */
export function placeTile(doc: DDefinition, layout: { id: string; grid: GridBox }[] | null) {
  if (!layout) return
  const where = new Map(layout.map((t) => [t.id, t.grid]))
  for (const tile of tilesOf(doc)) {
    const g = where.get(tile.id)
    if (g && (g.x !== tile.grid.x || g.y !== tile.grid.y || g.w !== tile.grid.w || g.h !== tile.grid.h)) tile.grid = { ...g }
  }
}

// ── new tiles ────────────────────────────────────────────────────────────────
const NEW_SIZE: Record<TileKind, { w: number; h: number }> = {
  kpi: { w: 3, h: 3 }, chart: { w: 6, h: 6 }, table: { w: 6, h: 6 }, matrix: { w: 6, h: 6 }, text: { w: 3, h: 2 }, image: { w: 3, h: 3 },
}
export const sizeOfNew = (kind: TileKind) => NEW_SIZE[kind]

function freeTileId(doc: DDefinition, prefix: string): string {
  const taken = new Set(tilesOf(doc).map((t) => t.id))
  for (let i = 1; ; i++) if (!taken.has(`${prefix}${i}`)) return `${prefix}${i}`
}

const isNumber = (f: DField) => fieldKind(f.type) === 'number'
/** What a new tile adds up: the last field of numbers, which is seldom a code as the first often is. */
const measureOf = (fields: DField[]) => [...fields].reverse().find(isNumber) ?? null
const summed = (f: DField | null) => (f ? `SUM(${fieldRef(f.name)})` : 'COUNT()')
const fieldsOfDataset = (doc: DDefinition, name: string) => doc.datasets.find((d) => d.name === name)?.fields ?? []

/** A new tile, on the first dataset, with data on it from the start: what is added up and along what
 *  come from its fields, for the person to change. */
export function newTile(doc: DDefinition, kind: TileKind, at: { x: number; y: number }, dataset?: string): DTile {
  const ds = dataset ?? doc.datasets[0]?.name ?? ''
  const fields = fieldsOfDataset(doc, ds)
  const measure = measureOf(fields)
  const words = fields.filter((f) => !isNumber(f))
  const base = { id: freeTileId(doc, kind), grid: { ...at, ...NEW_SIZE[kind] } }
  switch (kind) {
    case 'kpi':
      return { ...base, type: 'kpi', dataset: ds, label: measure?.name ?? '', value: fields.length ? summed(measure) : '' }
    case 'chart':
      return {
        ...base, type: 'chart', kind: 'column', dataset: ds, category: words[0] ? fieldRef(words[0].name) : '',
        series: [fields.length ? { value: summed(measure), name: measure?.name ?? '' } : { value: '' }], options: {},
      }
    case 'table':
      return { ...base, type: 'table', dataset: ds, columns: fields.slice(0, 6).map(columnFor), groups: [], detail: true, zebra: false, grand_total: false }
    case 'matrix':
      return {
        ...base, type: 'table', dataset: ds, columns: [{ header: words[0]?.name ?? '', value: '' }],
        groups: [{ by: words[0] ? fieldRef(words[0].name) : '', header: false, footer: false }], detail: false,
        matrix: { columns_by: words[1] ? fieldRef(words[1].name) : '', value: fields.length ? summed(measure) : '', total: false },
      }
    case 'text':
      return { ...base, type: 'text', value: '' }
    case 'image':
      return { ...base, type: 'image', asset: '', fit: 'contain' }
  }
}

/** A copy of a tile with its own id, where it first fits. */
export function duplicateTile(doc: DDefinition, tile: DTile): DTile {
  const copy = JSON.parse(JSON.stringify(tile)) as DTile
  copy.id = freeTileId(doc, tile.type === 'table' && tile.matrix ? 'matrix' : tile.type)
  copy.grid = { ...freeSpot(tilesOf(doc), tile.grid.w, tile.grid.h), w: tile.grid.w, h: tile.grid.h }
  return copy
}

/** A field clicked or dropped on a tile joins it as what it can be there; false when it cannot. */
export function fieldOnTile(tile: Record<string, any>, dataset: string, field: DField): boolean {
  if (tile.dataset && tile.dataset !== dataset) return false
  const ref = fieldRef(field.name)
  const number = isNumber(field)
  if (tile.type === 'table') {
    tile.dataset = dataset
    if (tile.matrix) {
      if (number) tile.matrix.value = summed(field)
      else if (!tile.groups?.[0]?.by) tile.groups = [{ by: ref, header: false, footer: false }]
      else tile.matrix.columns_by = ref
    } else {
      (tile.columns ??= []).push(columnFor(field))
    }
    return true
  }
  if (tile.type === 'chart') {
    tile.dataset = dataset
    if (!number) { tile.category = ref; return true }
    const free = tile.series?.find((s: any) => !s.value?.trim())
    if (free) Object.assign(free, { value: summed(field), name: field.name })
    else (tile.series ??= []).push({ value: summed(field), name: field.name })
    return true
  }
  if (tile.type === 'kpi' && number) {
    tile.dataset = dataset
    tile.value = summed(field)
    if (!tile.label) tile.label = field.name
    return true
  }
  return false
}

// ── what the live view asks ──────────────────────────────────────────────────
const written = (v: unknown) => typeof v === 'string' && !!v.trim()

/** Whether a tile says enough to be asked: one half made would stop the whole request, and it is
 *  being made, which is no mistake. What it says is judged by the server. */
export function readyToAsk(doc: Pick<DDefinition, 'datasets'>, tile: Record<string, any>): boolean {
  if (tile.type === 'image') return written(tile.asset)
  if (tile.type === 'text') return written(tile.value) && (!tile.dataset || doc.datasets.some((d) => d.name === tile.dataset))
  if (!doc.datasets.some((d) => d.name === tile.dataset)) return false
  if (tile.type === 'kpi') return written(tile.value)
  if (tile.type === 'chart') {
    return (['gauge', 'bullet'].includes(tile.kind) || written(tile.category)) && !!tile.series?.length && tile.series.every((s: any) => written(s.value))
  }
  if (tile.type === 'table') {
    if (tile.matrix) return written(tile.matrix.columns_by) && written(tile.matrix.value) && !!tile.groups?.length && tile.groups.every((g: any) => written(g.by))
    return !!tile.columns?.length && tile.columns.every((c: any) => written(c.value))
  }
  return false
}

/** What each tile's numbers depend on. Its place and its title are not among them: the screen knows
 *  those, and on ClickHouse or BigQuery a read costs money. */
export function previewSignatures(d: DashboardDefinition, values: Record<string, unknown>): Record<string, string> {
  // the fields of a dataset are a help to who writes: the data does not change when they are read
  const sources = d.datasets.map(({ fields: _f, ...source }) => source)
  const around = JSON.stringify([d.locale, d.timezone, d.parameters, sources, values])
  return Object.fromEntries(d.tiles.map((t) => {
    const { grid: _g, title: _t, ...said } = t
    return [t.id, `${around}|${JSON.stringify(said)}`]
  }))
}

/** The tiles whose numbers no longer answer what is asked. */
export const tilesChanged = (before: Record<string, string>, now: Record<string, string>): string[] =>
  Object.keys(now).filter((id) => before[id] !== now[id])

// ── the problems of the server ───────────────────────────────────────────────
/** The problems of a refused save or of the check, where the report designer's panels show them. A
 *  tile's path is the one of the item it is in the store. */
export function problemsOfBoard(doc: DDefinition, detail: unknown): DesignProblem[] {
  const rewrite = (raw: any) => {
    if (!raw || typeof raw !== 'object') return raw
    const out = { ...raw }
    for (const key of ['where', 'field']) {
      if (typeof out[key] === 'string') out[key] = out[key].replace(/^tiles\[(\d+)\]/, 'sections.body.items[$1]')
    }
    return out
  }
  if (!detail || typeof detail !== 'object') return problemsOf(doc, detail)
  const d = rewrite(detail)
  if (Array.isArray(d.problems)) d.problems = d.problems.map(rewrite)
  if (Array.isArray(d.errors)) d.errors = d.errors.map(rewrite)
  return problemsOf(doc, d)
}
