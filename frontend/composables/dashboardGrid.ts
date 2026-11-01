// A dashboard's grid at the three widths of a screen (docs/specs/dashboards.md, «The grid»).
// Pure functions, tested without a browser (tests/dashboards.test.mjs); the viewer and the
// designer draw what they return.
//
// A tile is drawn on 12 columns. On 6 its width is halved, rounding up, which is more room than
// the halves of a row had: four quarters are 2 + 2 + 2 + 2 of 6. So the tiles are laid again in
// the order drawn, each as near to where it was as it fits, and never on another; then each slides
// left and takes what is free beside it and under it, so that no row ends in a hole.

export const GRID_COLUMNS = 12
export const MEDIUM_COLUMNS = 6
export const GRID_ROW_PX = 40
export const GRID_GAP_PX = 10
export const WIDE_FROM_PX = 1100
export const MEDIUM_FROM_PX = 700
// the gateway's (services/dashboards.py), which refuses past them
export const MAX_GRID_ROW = 1000
export const MAX_TILE_ROWS = 100
/** The smallest tile of each type, in columns and rows. */
export const MIN_TILE: Record<string, [number, number]> = { kpi: [2, 2], chart: [4, 4], table: [4, 4], text: [2, 1], image: [1, 1] }

export type GridWidth = 'wide' | 'medium' | 'narrow'
export interface GridBox { x: number; y: number; w: number; h: number }
export interface GridTile { id: string; type?: string; grid: GridBox }
export interface PlacedTile extends GridBox { id: string }

export const gridWidth = (px: number): GridWidth => (px >= WIDE_FROM_PX ? 'wide' : px >= MEDIUM_FROM_PX ? 'medium' : 'narrow')
export const columnsOf = (width: GridWidth) => (width === 'wide' ? GRID_COLUMNS : width === 'medium' ? MEDIUM_COLUMNS : 1)

// on a phone a chart or a table drawn small on a large screen could not be read: at least this many rows
const NARROW_ROWS: Record<string, number> = { chart: 6, table: 8 }

const inReadingOrder = <T extends GridTile>(tiles: T[]) => [...tiles].sort((a, b) => a.grid.y - b.grid.y || a.grid.x - b.grid.x)
const collides = (a: GridBox, b: GridBox) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

/** Where the bands only hidden tiles held were: the rows, closed up. As drawn otherwise. */
function drawn(tiles: GridTile[], hidden: Set<string>): PlacedTile[] {
  const shown = tiles.filter((t) => !hidden.has(t.id))
  const held = new Set(shown.flatMap((t) => Array.from({ length: t.grid.h }, (_, i) => t.grid.y + i)))
  const gone = [...new Set(tiles.filter((t) => hidden.has(t.id))
    .flatMap((t) => Array.from({ length: t.grid.h }, (_, i) => t.grid.y + i)))].filter((row) => !held.has(row))
  return shown.map((t) => ({ id: t.id, ...t.grid, y: t.grid.y - gone.filter((row) => row < t.grid.y).length }))
}

function onMedium(tiles: PlacedTile[]): PlacedTile[] {
  const placed: PlacedTile[] = []
  const free = (box: GridBox) => !placed.some((p) => collides(p, box))
  let floor = 0 // never above the tile before it: the order is the one drawn
  for (const t of tiles) {
    const w = Math.ceil(t.w / 2)
    const last = MEDIUM_COLUMNS - w
    const wanted = Math.min(Math.floor(t.x / 2), last)
    // the row it reaches first: to its right, then to its left. A row further down starts over
    // from the left: a tile that wrapped has no column to keep
    const onItsRow = [...Array.from({ length: last - wanted + 1 }, (_, i) => wanted + i),
      ...Array.from({ length: wanted }, (_, i) => wanted - 1 - i)]
    const fromLeft = Array.from({ length: last + 1 }, (_, i) => i)
    // the only rows where a tile can first fit: this one, and where another ends
    const rows = [...new Set([floor, ...placed.map((p) => p.y + p.h).filter((y) => y > floor)])].sort((a, b) => a - b)
    let spot: PlacedTile | null = null
    for (const y of rows) {
      const x = (y === floor ? onItsRow : fromLeft).find((c) => free({ x: c, y, w, h: t.h }))
      if (x !== undefined) {
        spot = { id: t.id, x, y, w, h: t.h }
        break
      }
    }
    placed.push(spot!) // the row after the last tile is always free
    floor = spot!.y
  }
  // no hole at the start or at the end of a row, nor under a tile shorter than those beside it. The
  // rows grow only as far as another tile already goes: the page does not get longer for this
  const reading = [...placed].sort((a, b) => a.y - b.y || a.x - b.x)
  for (const p of reading) {
    const others = placed.filter((o) => o !== p)
    const clear = (box: GridBox) => !others.some((o) => collides(o, box))
    while (p.x > 0 && clear({ ...p, x: p.x - 1, w: 1 })) p.x--
    while (p.x + p.w < MEDIUM_COLUMNS && clear({ ...p, x: p.x + p.w, w: 1 })) p.w++
  }
  for (const p of reading) {
    const others = placed.filter((o) => o !== p)
    const below = () => ({ ...p, y: p.y + p.h, h: 1 })
    while (others.some((o) => o.y <= p.y + p.h && p.y + p.h < o.y + o.h) && !others.some((o) => collides(o, below()))) p.h++
  }
  return placed
}

/**
 * The tiles at a width, in reading order, which is also the order of the page for the keyboard
 * and for whoever listens to it. `hidden`: those their condition hides, which take no place.
 */
export function placeTiles(tiles: GridTile[], width: GridWidth, hidden: Set<string> = new Set()): PlacedTile[] {
  const types = new Map(tiles.map((t) => [t.id, t.type ?? '']))
  const shown = drawn(inReadingOrder(tiles), hidden)
  if (width === 'wide') return shown
  if (width === 'medium') return onMedium(shown).sort((a, b) => a.y - b.y || a.x - b.x)
  let y = 0
  return shown.map((t) => {
    const h = Math.max(t.h, NARROW_ROWS[types.get(t.id) ?? ''] ?? 0)
    const placed = { id: t.id, x: 0, y, w: 1, h }
    y += h
    return placed
  })
}

// ── the designer: drawn on 12 columns, the tiles push each other ─────────────
export type GridHandle = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'

/** How many cells a pointer went, on a grid `width` pixels wide: by the sizes the viewer draws with. */
export function cellsOf(dx: number, dy: number, width: number): { cols: number; rows: number } {
  const across = (width + GRID_GAP_PX) / GRID_COLUMNS
  return { cols: Math.round(dx / across) || 0, rows: Math.round(dy / (GRID_ROW_PX + GRID_GAP_PX)) || 0 }
}

/** A tile moved by some cells, inside the columns and under the first row. */
export function boxMoved(box: GridBox, cols: number, rows: number): GridBox {
  return {
    ...box,
    x: Math.max(0, Math.min(box.x + cols, GRID_COLUMNS - box.w)),
    y: Math.max(0, Math.min(box.y + rows, MAX_GRID_ROW)),
  }
}

/** A tile with a handle dragged some cells: the edges of that handle move, the others stay, down to
 *  the smallest tile of its type and up to the grid's edges. */
export function boxResized(box: GridBox, handle: GridHandle, cols: number, rows: number, type = ''): GridBox {
  const [leastW, leastH] = MIN_TILE[type] ?? [1, 1]
  let [left, top, right, bottom] = [box.x, box.y, box.x + box.w, box.y + box.h]
  if (handle.includes('w')) left = Math.max(0, Math.min(left + cols, right - leastW))
  if (handle.includes('e')) right = Math.min(GRID_COLUMNS, Math.max(right + cols, left + leastW))
  if (handle.includes('n')) top = Math.max(0, bottom - MAX_TILE_ROWS, Math.min(top + rows, bottom - leastH))
  if (handle.includes('s')) bottom = Math.min(top + MAX_TILE_ROWS, Math.max(bottom + rows, top + leastH))
  return { x: left, y: top, w: right - left, h: bottom - top }
}

/** The layout with one tile at `box`: whatever it lands on goes down to the first row free, and so
 *  on in reading order, so no two tiles share a cell and none goes up. A tile not in the way stays the
 *  same object. A gesture calls it from the layout it started with, at every cell: what was pushed
 *  goes back when the tile has moved on. Null when a tile would pass the last row the server takes. */
export function pushed<T extends GridTile>(tiles: T[], id: string, box: GridBox): T[] | null {
  const placed: GridBox[] = [box]
  const moved = new Map<string, GridBox>([[id, box]])
  for (const t of inReadingOrder(tiles.filter((x) => x.id !== id))) {
    let y = t.grid.y
    for (;;) {
      const hits = placed.filter((p) => collides(p, { ...t.grid, y }))
      if (!hits.length) break
      y = Math.max(...hits.map((p) => p.y + p.h))
    }
    if (y > MAX_GRID_ROW) return null
    if (y !== t.grid.y) moved.set(t.id, { ...t.grid, y })
    placed.push({ ...t.grid, y })
  }
  return tiles.map((t) => (moved.has(t.id) ? { ...t, grid: moved.get(t.id)! } : t))
}

/** The row under the last tile. */
export const lowestRow = (tiles: GridTile[]) => tiles.reduce((most, t) => Math.max(most, t.grid.y + t.grid.h), 0)

/** Where a new tile of this size first fits, reading the grid: the rows that can hold it are the
 *  first, and those where another ends. */
export function freeSpot(tiles: GridTile[], w: number, h: number): { x: number; y: number } {
  const rows = [...new Set([0, ...tiles.map((t) => t.grid.y + t.grid.h)])].sort((a, b) => a - b)
  for (const y of rows) {
    for (let x = 0; x + w <= GRID_COLUMNS; x++) {
      if (!tiles.some((t) => collides(t.grid, { x, y, w, h }))) return { x, y }
    }
  }
  return { x: 0, y: lowestRow(tiles) } // never reached: the row under the last tile is free
}
