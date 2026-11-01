// A click that filters the other tiles (docs/specs/dashboards.md, «A click filters the others»): what
// is chosen, how the address writes it, what a click on a chart names, what the strip says. Pure
// functions, tested without a browser; the page (pages/dashboard.vue) calls them.
//
// The browser holds the raw value a tile gave (`keys`, a row's `path`, a header's `key`), never the
// text on the screen, and sends it back with the tile's id: the expression stays on the server. An
// address has only texts: `?click.by_area=Nord-Est`, the value's text; an empty group is `{none}`, and
// a value of two levels its JSON, `["Nord",2023]`. The server finds a number or a date by its text,
// and says in `chosen` what it found: a value no longer there goes, and with none left the click.

// a type only: useDashboards.ts is the API's, this one is loaded bare by the tests
import type { TileAnswer } from '~/composables/useDashboards'

export type ClickKey = string | number | boolean | null
export type ClickValue = ClickKey | ClickKey[]
export type ClickSide = 'rows' | 'columns'
export interface TileClick { tile: string; on: ClickSide; values: ClickValue[] }

export const MAX_CLICK_VALUES = 100 // the gateway's (services/dashboards.py)
const NOTHING: ClickValue[] = Object.freeze([]) as unknown as ClickValue[] // the same one: no tile is drawn again for it

// ── which tile can ───────────────────────────────────────────────────────────
export function canFilter(tile: Record<string, any>, on: ClickSide = 'rows'): boolean {
  if (tile.filters_others === false) return false
  if (tile.type === 'chart') return on === 'rows' && !!tile.category && tile.kind !== 'gauge'
  if (tile.type !== 'table') return false
  return on === 'rows' ? !!tile.groups?.length : !!tile.matrix?.columns_by?.length
}
export const hasFilters = (tiles: Record<string, any>[]) => tiles.some((t) => canFilter(t) || canFilter(t, 'columns'))

// ── one value ────────────────────────────────────────────────────────────────
const pathOf = (v: ClickValue): ClickKey[] => (Array.isArray(v) ? v : [v])
/** What makes two values one: a number from an address and the same number from a chart are. */
export const valueText = (v: ClickValue) => JSON.stringify(pathOf(v).map((x) => (x === null ? null : String(x))))
const plainKey = (x: unknown): x is ClickKey => x === null || ['string', 'number', 'boolean'].includes(typeof x)

export function writeClick(v: ClickValue): string {
  if (v === null) return '{none}'
  if (Array.isArray(v)) return JSON.stringify(v)
  const text = String(v)
  return typeof v === 'string' && (text === '{none}' || text.startsWith('[') || text.startsWith('\\')) ? `\\${text}` : text
}
export function readClick(text: string): ClickValue {
  if (text === '{none}') return null
  if (text.startsWith('\\')) return text.slice(1)
  if (text.startsWith('[')) {
    try {
      const path = JSON.parse(text)
      if (Array.isArray(path) && path.length && path.every(plainKey)) return path
    } catch { /* a text that starts like one */ }
  }
  return text
}

// ── the address ──────────────────────────────────────────────────────────────
const COLUMNS = '.columns'
const addressKey = (c: Pick<TileClick, 'tile' | 'on'>) => `click.${c.tile}${c.on === 'columns' ? COLUMNS : ''}`

/** The clicks of an address that can still be: a tile gone, or switched off, is left out, so that an
 *  old link opens without that filter. */
export function clicksInQuery(query: Record<string, unknown>, tiles: Record<string, any>[]): TileClick[] {
  const byId = new Map(tiles.map((t) => [t.id as string, t]))
  const out: TileClick[] = []
  for (const [key, raw] of Object.entries(query)) {
    if (!key.startsWith('click.')) continue
    const name = key.slice('click.'.length)
    const on: ClickSide = name.endsWith(COLUMNS) ? 'columns' : 'rows'
    const id = on === 'columns' ? name.slice(0, -COLUMNS.length) : name
    const tile = byId.get(id)
    const texts = (Array.isArray(raw) ? raw : [raw]).filter((x): x is string => typeof x === 'string')
    if (tile && canFilter(tile, on) && texts.length) out.push({ tile: id, on, values: texts.slice(0, MAX_CLICK_VALUES).map(readClick) })
  }
  return out
}

export function clicksQuery(clicks: TileClick[]): Record<string, string | string[]> {
  const q: Record<string, string | string[]> = {}
  for (const c of clicks) q[addressKey(c)] = c.values.length === 1 ? writeClick(c.values[0]) : c.values.map(writeClick)
  return q
}

const clicksKey = (clicks: TileClick[]) => JSON.stringify(clicks.map((c) => [addressKey(c), c.values.map(valueText).sort()]).sort())
export const sameClicks = (a: TileClick[], b: TileClick[]) => clicksKey(a) === clicksKey(b)

/** As the request sends them: a tile's id and values, and never an expression. */
export const clickSends = (clicks: TileClick[]) => clicks.map((c) => ({ tile: c.tile, ...(c.on === 'columns' ? { on: c.on } : {}), values: c.values }))

export function clicksOfTile(clicks: TileClick[], tile: string, on: ClickSide = 'rows'): ClickValue[] {
  return clicks.find((c) => c.tile === tile && c.on === on)?.values ?? NOTHING
}

// ── a click, and what it changes ─────────────────────────────────────────────
/** After a click: a plain one chooses this value alone, or clears it if it was the only one; with
 *  `add` (Ctrl, Cmd) it is added, or taken away. The other tiles' stay. */
export function clickAfter(clicks: TileClick[], tile: string, on: ClickSide, value: ClickValue, add: boolean): TileClick[] {
  const text = valueText(value)
  const now = clicks.find((c) => c.tile === tile && c.on === on)
  const has = !!now?.values.some((v) => valueText(v) === text)
  let values: ClickValue[]
  if (add) {
    if (!has && (now?.values.length ?? 0) >= MAX_CLICK_VALUES) return clicks
    values = has ? now!.values.filter((v) => valueText(v) !== text) : [...(now?.values ?? []), value]
  } else {
    values = has && now!.values.length === 1 ? [] : [value]
  }
  const others = clicks.filter((c) => c !== now)
  return values.length ? [...others, { tile, on, values }] : others
}

/** The tiles as the server reads them: a text need not name a dataset, and with only one it reads that. */
export function namingTheDataset(tiles: Record<string, any>[], datasets: Record<string, any>[]): Record<string, any>[] {
  return datasets.length === 1 ? tiles.map((t) => (t.type === 'text' && !t.dataset ? { ...t, dataset: datasets[0].name } : t)) : tiles
}

/** The tiles a click on this one changes: the others on its dataset, and itself, which says what is chosen. */
export function tilesItChanges(tiles: Record<string, any>[], tile: string): string[] {
  const dataset = tiles.find((t) => t.id === tile)?.dataset
  return dataset ? tiles.filter((t) => t.dataset === dataset).map((t) => t.id as string) : []
}

/** Going from some clicks to others: the tiles to ask again, and those of them (`still`) whose
 *  numbers stay, as nothing that filters them changed. Those keep their look while they are asked. */
export function askedAfterClicks(tiles: Record<string, any>[], before: TileClick[], after: TileClick[]): { ids: string[]; still: string[] } {
  const of = (list: TileClick[]) => new Map(list.map((c) => [addressKey(c), clicksKey([c])]))
  const [was, is] = [of(before), of(after)]
  const changed = [...new Set([...before, ...after].filter((c) => was.get(addressKey(c)) !== is.get(addressKey(c))).map((c) => c.tile))]
  const ids = [...new Set(changed.flatMap((id) => tilesItChanges(tiles, id)))]
  const datasetOf = new Map(tiles.map((t) => [t.id as string, t.dataset]))
  const still = ids.filter((id) => !changed.some((other) => other !== id && datasetOf.get(other) === datasetOf.get(id)))
  return { ids, still }
}

/** What an answer says of the clicks, or null when it changes nothing: a tile says what is `chosen`
 *  and still there, and one the gateway refused (it may not be read, or cannot be resolved) counted
 *  for nothing. An error of another kind says nothing of what is chosen, nor does a tile hidden or
 *  not in this answer. */
export function settledClicks(clicks: TileClick[], tiles: Record<string, TileAnswer | any>): TileClick[] | null {
  let changed = false
  const out: TileClick[] = []
  for (const c of clicks) {
    const answer = tiles[c.tile]
    if (answer?.code === 'no_access' || answer?.code === 'dataset') {
      changed = true
      continue
    }
    const found: ClickValue[] | undefined = answer?.chosen?.[c.on]
    if (!found || clicksKey([{ ...c, values: found }]) === clicksKey([c])) {
      out.push(c)
      continue
    }
    changed = true
    if (found.length) out.push({ ...c, values: found })
  }
  return changed ? out : null
}

// ── what was clicked, on a chart ─────────────────────────────────────────────
/** What a click on a chart names, from ECharts' event: the raw value of its category, with its
 *  series' on a split; undefined when it names nothing («Other», a waterfall's total, a gauge). A
 *  line's point is found by its place on the axis (components/dashboard/TileChart.vue). */
export function clickedValue(a: Record<string, any>, p: any): ClickValue | undefined {
  const d = a.data ?? {}
  const keys: ClickKey[] = d.keys ?? []
  const splits: ClickKey[] = d.split_keys ?? []
  const both = (at: number, part?: number): ClickValue | undefined => {
    if (at === undefined || at === null || at < 0 || at >= keys.length) return undefined
    return part !== undefined && part !== null && part < splits.length ? [keys[at], splits[part]] : keys[at]
  }
  switch (a.kind) {
    case 'gauge':
      return undefined
    case 'scatter': {
      const x = (Array.isArray(p.value) ? p.value : p.data?.value)?.[0]
      if (x === undefined) return undefined
      return splits.length ? [x, splits[p.seriesIndex]] : x
    }
    case 'heatmap': {
      const [x, y] = Array.isArray(p.value) ? p.value : []
      return both(x, y)
    }
    case 'pie':
    case 'donut':
    case 'funnel':
    case 'treemap':
      return both(p.data?.at, p.data?.part)
    case 'bullet':
      return both(p.seriesType === 'scatter' ? p.value?.[1] : p.dataIndex)
    case 'waterfall':
      return both(p.dataIndex)
    default:
      return both(p.dataIndex, splits.length ? p.seriesIndex : undefined)
  }
}

// ── what the strip says of a value ───────────────────────────────────────────
const samePath = (a: ClickValue, b: ClickValue) => valueText(a) === valueText(b)

/** The words of the tile for a value: the chart's or the row's or the header's, what was on the
 *  screen when it was clicked. Undefined when the answer on screen does not hold it (a table's other
 *  page, say): the page remembers what it was told at the click. */
export function clickLabel(answer: any, on: ClickSide, value: ClickValue): string | undefined {
  if (!answer || 'error' in answer) return undefined
  const path = pathOf(value)
  if (answer.type === 'chart') {
    const d = answer.data ?? {}
    const at = (d.keys ?? []).findIndex((k: ClickKey) => samePath(k, path[0]))
    if (at < 0) return undefined
    const cat = d.cats?.[at]
    if (path.length < 2) return cat
    const part = (d.split_keys ?? []).findIndex((k: ClickKey) => samePath(k, path[1]))
    return part < 0 ? cat : `${cat} · ${d.yCats?.[part] ?? d.series?.[part]?.name}`
  }
  if (on === 'columns') {
    const names: string[] = []
    for (let level = 0; level < path.length; level++) {
      const cell = answer.head?.[level]?.find((h: any) => h.key && samePath(h.key, path.slice(0, level + 1)))
      if (!cell) return undefined
      names.push(cell.t)
    }
    return names.join(' · ')
  }
  const rowName = (r: any) => (r.kind === 'd' ? r.cells?.[0]?.text : r.label)
  const names: string[] = []
  for (let level = 0; level < path.length; level++) {
    const row = answer.rows?.find((r: any) => r.path && samePath(r.path, path.slice(0, level + 1)))
    if (!row) return undefined
    names.push(rowName(row))
  }
  return names.join(' · ')
}
