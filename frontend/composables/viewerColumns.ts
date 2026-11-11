// The Viewer's table starts empty: the person builds it by dragging the datasource's fields
// onto three areas, Rows, Columns and Measures (pages/viewer.vue). This is the pure half of
// it: the list of fields, the areas and what they make, where a dropped field lands, which
// request a change needs, and what a saved view's areas are.
//
// The rule the requests follow: the table asks the engine ONLY the columns it shows (a
// `select` step at the end of the chain, so that filters and computed fields can still read
// columns that are not shown; parquet is columnar, three columns of eighty cost three),
// nothing while it shows none, and nothing again for what the rows on screen already hold:
// moving and removing a column are free, adding one is one request, and the row count does
// not depend on the columns.
import type { ColumnInfo, Operation } from './useApi'

export interface ViewerField {
  name: string
  dtype: string
  description: string
  // made by the view's computed fields (its type is known once a result held it)
  computed: boolean
}

/** The fields a view can show: the datasource's columns with their descriptions (the curated
 *  ones first, then the source's own), then the named computed fields. A computed field that
 *  takes a column's name replaces it where it stands, as the engine does. */
export function viewerFields(
  columns: { name: string; dtype: string; description?: string }[],
  descriptions: Record<string, string> | null | undefined,
  computed: { name: string; expr: string }[],
  knownTypes: Record<string, string> = {},
): ViewerField[] {
  const made = new Map<string, string>()
  for (const c of computed) {
    const name = c.name.trim()
    if (name && c.expr.trim()) made.set(name, c.expr.trim())
  }
  const out: ViewerField[] = columns.map((c) => made.has(c.name)
    ? { name: c.name, dtype: knownTypes[c.name] ?? '', description: made.get(c.name)!, computed: true }
    : { name: c.name, dtype: c.dtype, description: (descriptions?.[c.name] || c.description || '').trim(), computed: false })
  const have = new Set(out.map((f) => f.name))
  for (const [name, expr] of made) {
    if (!have.has(name)) out.push({ name, dtype: knownTypes[name] ?? '', description: expr, computed: true })
  }
  return out
}

/** A column moved by `delta` places (the keyboard's way), stopping at the ends. */
export function shiftColumn(chosen: string[], name: string, delta: number): string[] {
  const from = chosen.indexOf(name)
  if (from < 0) return chosen
  const to = Math.max(0, Math.min(chosen.length - 1, from + delta))
  if (to === from) return chosen
  const rest = chosen.filter((c) => c !== name)
  return [...rest.slice(0, to), name, ...rest.slice(to)]
}

/** Would dropping `name` at slot `at` change nothing (its own place, or the slot after it)? */
export function sameSlot(chosen: string[], name: string, at: number): boolean {
  const from = chosen.indexOf(name)
  return from >= 0 && (at === from || at === from + 1)
}

/** The slot a pointer at `x` would drop into, among headers given by their left and right
 *  edges in order: before the header whose middle is past the pointer, after the last one. */
export function insertionSlot(edges: { left: number; right: number }[], x: number): number {
  for (let i = 0; i < edges.length; i++) {
    if (x < (edges[i].left + edges[i].right) / 2) return i
  }
  return edges.length
}

/** The chosen columns that still exist, in their order, each once. */
export function keepKnown(chosen: unknown, available: string[]): string[] {
  if (!Array.isArray(chosen)) return []
  const known = new Set(available)
  const out: string[] = []
  for (const c of chosen) {
    if (typeof c === 'string' && known.has(c) && !out.includes(c)) out.push(c)
  }
  return out
}

/** The step that makes the engine read the shown columns only, in their order. */
export function selectStep(columns: string[]): Operation {
  return { type: 'select', params: { columns: [...columns] } }
}

/** What the rows depend on besides their columns: the datasource's snapshot, the engine, the
 *  filters and the computed fields. The same key = the same rows, the same count. */
export function baseKeyOf(ds: { id: number; key: string | null } | null, engine: string, baseOps: Operation[]): string {
  return JSON.stringify([ds?.id ?? null, ds?.key ?? null, engine, baseOps])
}

export interface RowsState {
  // the columns the table shows, in order
  chosen: string[]
  // the columns the rows on screen hold (more than the shown ones, after a removal)
  held: string[]
  // what the rows on screen were computed on (`baseKeyOf`); null with no rows
  heldKey: string | null
  // what the view computes now
  baseKey: string
  // the computed fields whose type no result has said yet: asked with the rows, not shown
  typeless: string[]
}
export interface RowsPlan {
  // the columns to ask the engine, null = no request at all
  columns: string[] | null
  // how many rows: the page's, or one when only the types of computed fields are asked
  onlyTypes: boolean
  // the rows on screen are of other filters: all of them are replaced (dimmed meanwhile)
  replaces: boolean
}

/** The rows request a change needs. `columns`: the table's columns changed (added, moved,
 *  removed); `apply`: the person pressed Apply (or a view opened), which always computes. */
export function rowsPlan(s: RowsState, why: 'columns' | 'apply'): RowsPlan {
  const none: RowsPlan = { columns: null, onlyTypes: false, replaces: false }
  const want = [...s.chosen, ...s.typeless.filter((c) => !s.chosen.includes(c))]
  if (!s.chosen.length) {
    // an empty table asks no rows; Apply learns the types of the computed fields, if any
    if (why !== 'apply' || !s.typeless.length) return none
    return { columns: [...s.typeless], onlyTypes: true, replaces: false }
  }
  const same = s.heldKey === s.baseKey
  if (why === 'columns') {
    const held = new Set(s.held)
    // everything shown is on screen already: moving, removing, putting back
    if (s.heldKey !== null && s.chosen.every((c) => held.has(c))) return none
  }
  return { columns: want, onlyTypes: false, replaces: s.heldKey !== null && (why === 'apply' || !same) }
}

/** Is a count to be asked for rows that came cut at the page? Not when the same filters were
 *  counted already (the columns do not change how many rows there are), unless the person
 *  asked to compute again. */
export function countNeeded(countedKey: string | null, baseKey: string, why: 'columns' | 'apply'): boolean {
  return why === 'apply' || countedKey !== baseKey
}

/** The columns whose profile is still to ask: the shown ones not profiled on these rows. */
export function profileMissing(chosen: string[], profiled: string[], sameRows: boolean): string[] {
  if (!sameRows) return [...chosen]
  const have = new Set(profiled)
  return chosen.filter((c) => !have.has(c))
}

/** The shown columns as the grid wants them: the type the result said, else the list's. */
export function shownColumns(chosen: string[], fields: ViewerField[], resultTypes: Record<string, string>): ColumnInfo[] {
  const byName = new Map(fields.map((f) => [f.name, f.dtype]))
  return chosen.map((name) => ({ name, dtype: resultTypes[name] || byName.get(name) || '' }))
}

/** Where a field dropped on the chart goes when the person does not say: a number is a value,
 *  anything else the axis. */
export function chartSlotOf(dtype: string): 'axis' | 'value' {
  return /Int|Float|Decimal/i.test(dtype) ? 'value' : 'axis'
}

// ── The three areas: Rows, Columns, Measures ──────────────────────────────────────
// The table is built as a cross table, by dragging fields onto three areas (the faces of
// the engine's pivot and group by):
//   Rows alone                    the rows as they are, those columns (the engine reads only them)
//   Rows + Measures               one row per group, the measures aggregated (a group by)
//   Measures alone                one row of totals
//   Columns + a measure (± Rows)  a cross table (the engine's pivot: ONE measure)
// The engine aggregates, always: no rows are brought to the browser to be added up.

export type ShelfArea = 'rows' | 'cols' | 'measures'
export interface Measure { column: string; func: string }
export interface Shelves { rows: string[]; cols: string[]; measures: Measure[] }
export type TableMode = 'empty' | 'detail' | 'grouped' | 'totals' | 'cross' | 'needs-measure'

/** The functions a measure can take: the ones the engine's group by and pivot share. */
export const MEASURE_FUNCS = ['sum', 'mean', 'min', 'max', 'count', 'n_unique'] as const

/** A field dropped on Measures: a number is summed, anything else counted. */
export function defaultFunc(dtype: string): string {
  return /Int|Float|Decimal/i.test(dtype) ? 'sum' : 'count'
}

export function tableMode(s: Shelves): TableMode {
  if (s.cols.length) return s.measures.length ? 'cross' : 'needs-measure'
  if (s.measures.length) return s.rows.length ? 'grouped' : 'totals'
  return s.rows.length ? 'detail' : 'empty'
}

/** The names the measures' columns take in a grouped table: `sum_ricavo`, each one once. */
export function measureAliases(measures: Measure[], taken: string[] = []): string[] {
  const used = new Set(taken)
  return measures.map((m) => {
    const base = `${m.func}_${m.column}`
    let alias = base
    for (let i = 2; used.has(alias); i++) alias = `${base}_${i}`
    used.add(alias)
    return alias
  })
}

const TOTALS_KEY = '__all'

/** The steps that aggregate, after the filters and the computed fields; none for the rows
 *  as they are (`detail`), for an empty table, and for Columns still without a measure. A
 *  cross table takes the first measure: the engine's pivot has one. */
export function shelfOps(s: Shelves): Operation[] {
  switch (tableMode(s)) {
    case 'cross': {
      const m = s.measures[0]
      return [{ type: 'pivot', params: { index: [...s.rows], on: [...s.cols], values: m.column, func: m.func } }]
    }
    case 'grouped': {
      const aliases = measureAliases(s.measures, s.rows)
      return [
        { type: 'group_by', params: { by: [...s.rows], aggregations: s.measures.map((m, i) => ({ column: m.column, func: m.func, alias: aliases[i] })) } },
        // a group by gives its groups in no order: by the rows' fields, so that the same view reads the same twice
        { type: 'sort', params: { by: [...s.rows], descending: false } },
      ]
    }
    case 'totals': {
      const aliases = measureAliases(s.measures, [TOTALS_KEY])
      return [
        // a group by with no key is not something every engine takes: one constant group
        { type: 'compute', params: { columns: [{ name: TOTALS_KEY, expr: '1' }] } },
        { type: 'group_by', params: { by: [TOTALS_KEY], aggregations: s.measures.map((m, i) => ({ column: m.column, func: m.func, alias: aliases[i] })) } },
        { type: 'select', params: { columns: aliases } },
      ]
    }
    default: return []
  }
}

/** Where a dragged field comes from when it is already on an area (its place there). */
export interface ShelfFrom { area: ShelfArea; index: number }

/** A field put on an area at slot `at` (left out: at the end). Coming from another area it
 *  leaves it; within its own it moves. A field is in Rows or in Columns, never in both, and
 *  once in each; a measure may repeat its field with another function. */
export function placeOnShelf(s: Shelves, area: ShelfArea, name: string, dtype: string, at?: number, from?: ShelfFrom | null): Shelves {
  let rows = [...s.rows]; let cols = [...s.cols]; let measures = s.measures.map((m) => ({ ...m }))
  let slot = at
  let func = defaultFunc(dtype)
  if (from) {
    if (from.area === 'measures') {
      const m = measures[from.index]
      if (m?.column === name) { func = m.func; measures.splice(from.index, 1) }
    } else {
      const list = from.area === 'rows' ? rows : cols
      if (list[from.index] === name) list.splice(from.index, 1)
    }
    // within its own area: the slot was counted with the field still in its place
    if (from.area === area && slot !== undefined && from.index < slot) slot -= 1
  }
  if (area === 'measures') {
    const same = measures.findIndex((m) => m.column === name && m.func === func)
    if (same >= 0) {
      if (slot === undefined) return s // «add» on a measure that is there: it stays
      measures.splice(same, 1)
      if (same < slot) slot -= 1
    }
    const to = Math.max(0, Math.min(slot ?? measures.length, measures.length))
    measures.splice(to, 0, { column: name, func })
  } else {
    const target = area === 'rows' ? rows : cols
    const there = target.indexOf(name)
    if (there >= 0) {
      if (slot === undefined) return s
      target.splice(there, 1)
      if (there < slot) slot -= 1
    }
    // the other of the two: a field is a row heading or a column heading
    if (area === 'rows') cols = cols.filter((c) => c !== name)
    else rows = rows.filter((c) => c !== name)
    const list = area === 'rows' ? rows : cols
    const to = Math.max(0, Math.min(slot ?? list.length, list.length))
    list.splice(to, 0, name)
  }
  return { rows, cols, measures }
}

export function removeFromShelf(s: Shelves, area: ShelfArea, index: number): Shelves {
  const out: Shelves = { rows: [...s.rows], cols: [...s.cols], measures: s.measures.map((m) => ({ ...m })) }
  out[area === 'measures' ? 'measures' : area].splice(index, 1)
  return out
}

/** A field taken off every area (the list of fields' Del). */
export function fieldOffShelves(s: Shelves, name: string): Shelves {
  return { rows: s.rows.filter((c) => c !== name), cols: s.cols.filter((c) => c !== name), measures: s.measures.filter((m) => m.column !== name) }
}

/** The areas less the fields that no longer exist (a computed field renamed, another datasource). */
export function keepKnownShelves(s: Shelves, available: string[]): Shelves {
  const known = new Set(available)
  return { rows: s.rows.filter((c) => known.has(c)), cols: s.cols.filter((c) => known.has(c)), measures: s.measures.filter((m) => known.has(m.column)) }
}

interface SpecLike {
  rows?: unknown; cols?: unknown; measures?: unknown; columns?: unknown
  pivotOn?: unknown
  pivot?: { index?: unknown; on?: unknown; values?: unknown; func?: unknown } | null
}
const names = (v: unknown): string[] => (Array.isArray(v) ? v.filter((c): c is string => typeof c === 'string' && c !== '') : [])

/** A saved view's three areas. A view saved with them has them (less the fields that are
 *  gone). A view of before has none: with its pivot on, the pivot's rows, columns and
 *  measure (the same request as before); without, every field as a row heading, that is
 *  the whole table it always opened with. */
export function shelvesOfSpec(spec: SpecLike | null | undefined, available: string[]): Shelves {
  const funcs = new Set<string>(MEASURE_FUNCS)
  if (spec && Array.isArray(spec.rows)) {
    const measures = (Array.isArray(spec.measures) ? spec.measures : [])
      .filter((m): m is Measure => !!m && typeof m.column === 'string' && typeof m.func === 'string' && funcs.has(m.func))
      .map((m) => ({ column: m.column, func: m.func }))
    return keepKnownShelves({ rows: keepKnown(spec.rows, available), cols: keepKnown(spec.cols, available), measures }, available)
  }
  const p = spec?.pivot
  const index = names(p?.index); const on = names(p?.on)
  if (spec?.pivotOn && p && typeof p.values === 'string' && p.values && (index.length || on.length)) {
    const func = typeof p.func === 'string' && funcs.has(p.func) ? p.func : 'sum'
    return keepKnownShelves({ rows: index, cols: on, measures: [{ column: p.values, func }] }, available)
  }
  return { rows: [...available], cols: [], measures: [] }
}

/** What a saved view keeps of the areas: themselves, and the pivot they are the face of in
 *  the shape the views always had (who reads a view elsewhere, a report or a dashboard,
 *  builds its chain from that: a cross table, or a group by with its first measure). */
export function specOfShelves(s: Shelves): { rows: string[]; cols: string[]; measures: Measure[]; pivotOn: boolean; pivot: { index: string[]; on: string[]; values: string; func: string } } {
  const m = s.measures[0]
  const on = !!m && (s.rows.length > 0 || s.cols.length > 0)
  return {
    rows: [...s.rows], cols: [...s.cols], measures: s.measures.map((x) => ({ ...x })),
    pivotOn: on,
    pivot: on ? { index: s.rows.length ? [...s.rows] : [''], on: s.cols.length ? [...s.cols] : [''], values: m.column, func: m.func }
      : { index: [''], on: [''], values: '', func: 'sum' },
  }
}
