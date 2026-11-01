// The numbers on a dashboard's screen, tile by tile: which answer counts, what stays after a
// failure, when to read again (docs/specs/dashboards.md, «On the screen»). Pure functions over
// `slots`, one per tile, tested without a browser; the page (pages/dashboard.vue) calls them.
//
// Every request has a number, and a tile listens only to the latest one that asked for it: an old
// answer arriving late, or after a newer one, changes nothing there, whatever it brings for
// the others. A slot that does not change stays the same object: its tile is not drawn again.

// a type only: useDashboards.ts is the API's, this one is loaded bare by the tests
import type { TileAnswer } from '~/composables/useDashboards'

export interface TileSlot {
  answer: TileAnswer | null
  /** when the numbers on screen were read from their source: it moves on, and they stay the same
   *  object, when the source says the same again */
  read?: string | null
  /** the latest request that asked for this tile */
  asked: number
  /** on its way: the same question again, or a new one (other values, a page, a sort) */
  waiting: false | 'same' | 'new'
  /** why the numbers on screen are not fresh: they stay, and the tile says so */
  trouble: string | null
}
export type TileSlots = Record<string, TileSlot>

export const hasNumbers = (slot: Pick<TileSlot, 'answer'>) => !!slot.answer && !('error' in slot.answer)

export function emptySlots(ids: string[]): TileSlots {
  return Object.fromEntries(ids.map((id) => [id, { answer: null, asked: 0, waiting: false, trouble: null }]))
}

/** Request `seq` asks for these tiles. `changed`: another question, which the numbers on screen
 *  do not answer; without it they are the same numbers, read again. `still`: those of them whose
 *  numbers do not change whatever else does, as the tile clicked keeps its bars. */
export function beginAsking(slots: TileSlots, ids: string[], seq: number, changed: boolean, still: string[] = []): TileSlots {
  const next = { ...slots }
  for (const id of ids) {
    if (next[id]) next[id] = { ...next[id], asked: seq, waiting: changed && !still.includes(id) ? 'new' : 'same' }
  }
  return next
}

/** The same numbers, whenever they were read: a chart is not drawn again for them. */
function sameNumbers(a: TileAnswer | null, b: TileAnswer): boolean {
  if (!a || 'error' in a || 'error' in b) return false
  const { computed_at: _at, cached: _kept, ...mine } = a
  const { computed_at: _at2, cached: _kept2, ...theirs } = b
  return JSON.stringify(mine) === JSON.stringify(theirs)
}

// a read that failed leaves the last numbers on screen; rights lost or a dataset past the cap do
// not: those numbers may not be shown any more
const KEPT_THROUGH: ReadonlySet<string> = new Set(['error', 'dataset'])

/** The answer of request `seq`. `keep`: the question is the one the numbers on screen answer;
 *  `still`: for these tiles it is, whatever the others were asked. */
export function tookAnswer(slots: TileSlots, seq: number, tiles: Record<string, TileAnswer>, opts: { keep: boolean; still?: string[] }): TileSlots {
  const next = { ...slots }
  for (const [id, slot] of Object.entries(slots)) {
    if (slot.asked !== seq) continue
    const answer = tiles[id]
    if (!answer) {
      next[id] = { ...slot, waiting: false }
    } else if ('error' in answer && (opts.keep || opts.still?.includes(id)) && hasNumbers(slot) && KEPT_THROUGH.has(answer.code)) {
      next[id] = { ...slot, waiting: false, trouble: answer.error }
    } else {
      const read = 'error' in answer ? null : answer.computed_at
      next[id] = { answer: sameNumbers(slot.answer, answer) ? slot.answer : answer, read, asked: seq, waiting: false, trouble: null }
    }
  }
  return next
}

/** Request `seq` found no answer at all. */
export function tookFailure(slots: TileSlots, seq: number, message: string, opts: { keep: boolean; still?: string[] }): TileSlots {
  const next = { ...slots }
  for (const [id, slot] of Object.entries(slots)) {
    if (slot.asked !== seq) continue
    next[id] = (opts.keep || opts.still?.includes(id)) && hasNumbers(slot)
      ? { ...slot, waiting: false, trouble: message }
      : { answer: { error: message, code: 'request' }, asked: seq, waiting: false, trouble: null }
  }
  return next
}

/** What a tile shows: `dim`, numbers that are not the answer to what is being asked. */
export function tileStates(slot: TileSlot) {
  const numbers = hasNumbers(slot)
  return { loading: !!slot.waiting, numbers, dim: slot.waiting === 'new' && numbers, trouble: slot.trouble }
}

/** The tiles that read data. One that reads none (an image, a text of plain words) has the time it
 *  was first worked out, which no refresh moves and which says nothing of the numbers. */
export function readingData(slots: TileSlots, tiles: Record<string, any>[]): TileSlots {
  const reading = new Set(tiles.filter((tile) => tile.dataset).map((tile) => tile.id as string))
  return Object.fromEntries(Object.entries(slots).filter(([id]) => reading.has(id)))
}

/** When the oldest numbers on screen were read from their source: the time a screen can say of
 *  all of them. None when there are no numbers. */
export function oldestRead(slots: Record<string, Pick<TileSlot, 'answer' | 'read'>>): string | null {
  let oldest: string | null = null
  for (const { answer, read } of Object.values(slots)) {
    if (!answer || 'error' in answer || answer.hidden) continue
    const at = read ?? answer.computed_at
    if (at && (oldest === null || Date.parse(at) < Date.parse(oldest))) oldest = at
  }
  return oldest
}

/**
 * A tick of the automatic refresh first takes what is shared: on ClickHouse and BigQuery a read
 * costs money, and a hundred people on one dashboard run one query. The kept numbers can outlive
 * the interval (DASHBOARDS__CACHE_SECONDS, 5 minutes by default, against an interval of one):
 * when the answer brings nothing newer than the screen had, whoever finds that out first asks the
 * source; the server lets one through every 10 seconds, and the others find it shared at their tick.
 */
export function askSourceAgain(before: Record<string, Pick<TileSlot, 'answer' | 'read'>>, after: Record<string, Pick<TileSlot, 'answer' | 'read'>>): boolean {
  const [was, is] = [oldestRead(before), oldestRead(after)]
  return was !== null && is !== null && Date.parse(is) <= Date.parse(was)
}

/** The pause before the next automatic refresh, in ms: the interval and up to a tenth more, so
 *  that the screens of a whole office do not ask at the same second. Never less: a tick before the
 *  interval's end finds the same numbers. */
export const refreshPause = (seconds: number, random: () => number = Math.random) =>
  Math.round(seconds * 1000 * (1 + 0.1 * random()))

/** The pages after the answer of request `seq`: a page past the last, the data having changed,
 *  comes back as the last one, and that is the page from then on. Only for the tables this is still
 *  the latest request of: a late answer does not undo the page asked for after it. */
export function pagesAfter(pages: Record<string, number>, slots: TileSlots, seq: number, tiles: Record<string, TileAnswer>): Record<string, number> {
  let next = pages
  for (const [id, answer] of Object.entries(tiles)) {
    if (slots[id]?.asked !== seq || 'error' in answer || answer.type !== 'table' || pages[id] === undefined || pages[id] === answer.page) continue
    next = { ...next, [id]: answer.page ?? 0 }
  }
  return next
}

/** The pages of the tiles asked for (`null`: all of them), left out those on their first. */
export function pagesFor(pages: Record<string, number>, ids: string[] | null): Record<string, number> {
  return Object.fromEntries(Object.entries(pages).filter(([id, page]) => page > 0 && (ids === null || ids.includes(id))))
}
