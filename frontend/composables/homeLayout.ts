// The home page's layout (docs/specs/home.md): each person's grid of widgets, kept by the
// gateway (GET/PUT /home, POST /home/reset). Pure functions, tested without a browser
// (tests/home.test.mjs); the page draws what they return. The grid is the dashboards'
// (dashboardGrid.ts): same columns, rows and gaps, and its rule for pushing what is in the way.
import { GRID_COLUMNS, boxMoved, boxResized, freeSpot, pushed, type GridBox, type GridHandle, type GridTile } from '~/composables/dashboardGrid'

export interface HomeWidget {
  id: string
  type: string
  x: number
  y: number
  w: number
  h: number
  options: Record<string, any>
}
export interface HomeAnswer {
  v: 1
  widgets: HomeWidget[]
  is_default: boolean
  /** the types this person may add, in the catalogue's order: the gateway's */
  available: string[]
}

/** The widgets this interface can draw. A type the gateway offers and this list lacks is left out. */
export const HOME_TYPES = ['clock', 'recent', 'failed_runs', 'shortcuts', 'note'] as const
/** The smallest widget of each type, in columns and rows: the gateway's (api/home.go). */
export const HOME_MIN: Record<string, [number, number]> = {
  clock: [2, 2], recent: [3, 4], failed_runs: [3, 4], shortcuts: [2, 2], note: [2, 2],
}
/** The size a new widget of each type starts with. */
export const HOME_NEW: Record<string, [number, number]> = {
  clock: [3, 3], recent: [6, 8], failed_runs: [6, 8], shortcuts: [6, 3], note: [4, 4],
}
/** The gateway's limits. */
export const HOME_MAX_WIDGETS = 30
export const HOME_MAX_ROW = 500
export const HOME_MAX_HEIGHT = 40

export const knownWidgets = (widgets: HomeWidget[]) => widgets.filter((w) => (HOME_TYPES as readonly string[]).includes(w.type))

const asTile = (w: HomeWidget): GridTile => ({ id: w.id, type: w.type, grid: { x: w.x, y: w.y, w: w.w, h: w.h } })
export const tilesOf = (widgets: HomeWidget[]) => widgets.map(asTile)

/** The layout with the grid's answer written back: the same widgets, at their new places. */
function placed(widgets: HomeWidget[], tiles: GridTile[]): HomeWidget[] {
  const at = new Map(tiles.map((t) => [t.id, t.grid]))
  return widgets.map((w) => {
    const g = at.get(w.id)
    return g && (g.x !== w.x || g.y !== w.y || g.w !== w.w || g.h !== w.h) ? { ...w, ...g } : w
  })
}

/** A box the gateway takes for this type: inside the columns, not under the smallest, not past the limits. */
export function fits(type: string, box: GridBox): boolean {
  const [minW, minH] = HOME_MIN[type] ?? [1, 1]
  return box.w >= minW && box.h >= minH && box.h <= HOME_MAX_HEIGHT && box.x >= 0 && box.x + box.w <= GRID_COLUMNS && box.y >= 0 && box.y <= HOME_MAX_ROW
}

/** One widget at `box`, whatever it lands on pushed down. Null when the box is not one the type
 *  may have, or when something would go past the last row. */
export function withBox(widgets: HomeWidget[], id: string, box: GridBox): HomeWidget[] | null {
  const w = widgets.find((x) => x.id === id)
  if (!w || !fits(w.type, box)) return null
  const tiles = pushed(tilesOf(widgets), id, box)
  if (!tiles || tiles.some((t) => t.grid.y > HOME_MAX_ROW)) return null
  return placed(widgets, tiles)
}

/** A widget moved by some cells (the keyboard's arrows, a drag). */
export function moved(widgets: HomeWidget[], id: string, cols: number, rows: number, from?: GridBox): HomeWidget[] | null {
  const w = widgets.find((x) => x.id === id)
  if (!w) return null
  return withBox(widgets, id, boxMoved(from ?? asTile(w).grid, cols, rows))
}

/** A widget resized by some cells from a handle; the smallest of its type holds. */
export function resized(widgets: HomeWidget[], id: string, handle: GridHandle, cols: number, rows: number, from?: GridBox): HomeWidget[] | null {
  const w = widgets.find((x) => x.id === id)
  if (!w) return null
  const start = from ?? asTile(w).grid
  const [minW, minH] = HOME_MIN[w.type] ?? [1, 1]
  const box = boxResized(start, handle, cols, rows)
  // boxResized knows the dashboards' smallest tiles, not these: hold the edge that moved
  if (box.w < minW) { if (handle.includes('w')) box.x = start.x + start.w - minW; box.w = minW }
  if (box.h < minH) { if (handle.includes('n')) box.y = start.y + start.h - minH; box.h = minH }
  if (box.h > HOME_MAX_HEIGHT) box.h = HOME_MAX_HEIGHT
  return withBox(widgets, id, box)
}

/** An id no widget has: the type and the first free number. */
export function newId(widgets: HomeWidget[], type: string): string {
  const base = type.replace(/_/g, '-')
  const taken = new Set(widgets.map((w) => w.id))
  if (!taken.has(base)) return base
  for (let n = 2; ; n++) if (!taken.has(`${base}-${n}`)) return `${base}-${n}`
}

/** A new widget of a type, where it first fits. Null at the limit. */
export function added(widgets: HomeWidget[], type: string): { widgets: HomeWidget[]; id: string } | null {
  if (widgets.length >= HOME_MAX_WIDGETS) return null
  const [w, h] = HOME_NEW[type] ?? [4, 4]
  const spot = freeSpot(tilesOf(widgets), w, h)
  const id = newId(widgets, type)
  return { widgets: [...widgets, { id, type, ...spot, w, h, options: {} }], id }
}

export const removed = (widgets: HomeWidget[], id: string) => widgets.filter((w) => w.id !== id)

export const withOptions = (widgets: HomeWidget[], id: string, options: Record<string, any>) =>
  widgets.map((w) => (w.id === id ? { ...w, options } : w))

/** What the gateway is sent: the layout alone. */
export const bodyOf = (widgets: HomeWidget[]) => ({
  v: 1,
  widgets: widgets.map(({ id, type, x, y, w, h, options }) => ({ id, type, x, y, w, h, options })),
})

/** Undo and redo of the layout while it is being arranged: whole layouts, a few dozen at most. */
export function history<T>(limit = 50) {
  let past: T[] = []
  let future: T[] = []
  return {
    /** call BEFORE a change, with the layout as it was */
    record(before: T) { past.push(before); if (past.length > limit) past.shift(); future = [] },
    undo(now: T): T | null { const p = past.pop(); if (p === undefined) return null; future.push(now); return p },
    redo(now: T): T | null { const f = future.pop(); if (f === undefined) return null; past.push(now); return f },
    canUndo: () => past.length > 0,
    canRedo: () => future.length > 0,
    clear() { past = []; future = [] },
  }
}

// ── the widgets' own small rules ─────────────────────────────────────────────

/** A shortcut is a page of this installation: the gateway's rule, so the form refuses first. */
export function internalPath(s: string): boolean {
  return typeof s === 'string' && s.length > 0 && s.length <= 200 && s.startsWith('/') && !s.startsWith('//') && !/[\\\u0000-\u001f\u007f]/.test(s)
}

/** The clock's face at an instant: the time and the date in a zone ('' = the browser's), in a language. */
export function clockFace(at: Date, o: { zone?: string; hour12?: boolean; seconds?: boolean }, locale: string): { time: string; date: string; zone: string } {
  const zone = o.zone || undefined
  const time = new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit', second: o.seconds ? '2-digit' : undefined, hour12: !!o.hour12, timeZone: zone }).format(at)
  const date = new Intl.DateTimeFormat(locale, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: zone }).format(at)
  return { time, date, zone: o.zone || Intl.DateTimeFormat().resolvedOptions().timeZone }
}

interface RunLike { launched_by: number | null; started_at: string | null; finished_at: string | null }
/** The failed runs a widget lists: within its days, and the person's own when it says «mine». */
export function failedFor<T extends RunLike>(runs: T[], o: { scope?: string; days?: number }, userId: number | null, now: number): T[] {
  const since = now - (o.days ?? 7) * 86_400_000
  return runs.filter((r) => {
    const at = Date.parse((r.finished_at ?? r.started_at ?? '') as string)
    return (Number.isNaN(at) || at >= since) && (o.scope !== 'mine' || (userId !== null && r.launched_by === userId))
  })
}
