// An AI comment on a dashboard's screen (docs/specs/ai-comment.md, «On the screen and on paper»): which
// tiles it reads, which answer counts, what stays while another is on its way. The page
// (pages/dashboard.vue) says when to ask; the moment goes with the request, and the gateway decides
// whether that makes a call. Tested with fakes, without a browser (tests/dashboards.test.mjs).
//
// - A slot is either a comment's answer or nothing (a skeleton). Asking does not touch what is on screen:
//   `blank` does, and a click or another value of a parameter do so before anything has answered. A refresh
//   keeps the text until there is another, so a screen on a wall does not flash.
// - An answer that says the same stays the same object: the tile is not drawn again for it.
// - Only the latest request of a tile counts, and a refresh does not knock down what is already asked: it
//   would only ask the same thing later.
// - With the tab hidden nothing is asked: the question waits for the tab (`resume`), once.
// - `pending`: someone is writing it, and past the wait the text will be kept. It is looked at again a few
//   times, at the moment first said, since the gateway makes no second call for it.

// a type only: useDashboards.ts is the API's, this one is loaded bare by the tests
import type { TileAnswer } from '~/composables/useDashboards'

export type CommentMoment = 'open' | 'refresh' | 'asked'
/** services/ai_comment.py, `Comment`. */
export interface TileComment {
  state: string
  text: string
  model: string
  numbers_at: string | null
  written_at: string | null
  stale: boolean
  can_ask: boolean
  message: string
  reason: string
}
export interface CommentSlot {
  answer: TileComment | null
  asking: false | CommentMoment
}
export type CommentSlots = Record<string, CommentSlot>

export const PENDING_TRIES = 6 // past CALL_SECONDS and its margin in the gateway, at the route's own wait
const PENDING_PAUSE_MS = 3000
const READABLE = new Set(['kpi', 'chart', 'table'])

export const commentIds = (tiles: Record<string, any>[]) => tiles.filter((t) => t.type === 'comment').map((t) => t.id as string)
/** What a comment can read: the gateway's COMMENTED_TYPES. */
export const readableBy = <T extends Record<string, any>>(tiles: T[]) => tiles.filter((t) => READABLE.has(t.type))
/** The comments that read any of these tiles. */
export const commentsReading = (tiles: Record<string, any>[], ids: string[]) =>
  tiles.filter((t) => t.type === 'comment' && (t.reads ?? []).some((name: string) => ids.includes(name))).map((t) => t.id as string)

/** A tile the model will never read: a table that only lists rows, a scatter that is not aggregated
 *  (`_only_rows` in services/ai_comment.py), from the numbers on screen. With none to look at (no answer yet, or
 *  a tile refused), what its definition can say. */
export function showsOnlyRows(tile: Record<string, any>, answer: TileAnswer | null | undefined): boolean {
  if (answer && !('error' in answer) && !answer.hidden) {
    if (answer.empty) return false
    if (tile.type === 'table') return !answer.matrix && (answer.rows ?? []).every((r: any) => r.kind === 'd')
    return answer.kind === 'scatter' && !answer.data?.aggregated
  }
  return tile.type === 'table' && !tile.matrix && !tile.groups?.length && !tile.grand_total
}

/** What stands in place of a comment when the question itself got no answer. */
export const unanswered = (message: string): TileComment => ({
  state: 'failed', text: '', model: '', numbers_at: null, written_at: null, stale: false, can_ask: false, message, reason: '',
})

const same = (a: TileComment | null, b: TileComment) => !!a && JSON.stringify(a) === JSON.stringify(b)
const isAbort = (e: any) => e?.name === 'AbortError' || e?.cause?.name === 'AbortError'

export interface CommentOptions {
  slots: () => CommentSlots
  setSlots: (s: CommentSlots) => void
  /** the tab is out of sight */
  hidden: () => boolean
  /** rejects as $fetch does */
  call: (tile: string, moment: CommentMoment, signal: AbortSignal) => Promise<TileComment>
  /** in place of a comment, when the request itself failed and there is no text to keep */
  failed: () => string
  /** what a person asked for has its answer */
  wrote?: (tile: string) => void
  /** the timers, so a test can do without them */
  later?: (run: () => void, ms: number) => number
  forget?: (timer: number) => void
}

export function createComments(o: CommentOptions) {
  const inFlight = new Map<string, AbortController>()
  const due = new Map<string, CommentMoment>()
  const timers = new Map<string, number>()
  const later = o.later ?? ((run, ms) => window.setTimeout(run, ms))
  const forget = o.forget ?? ((timer) => window.clearTimeout(timer))

  function patch(id: string, change: Partial<CommentSlot>) {
    const was = o.slots()[id] ?? { answer: null, asking: false as const }
    const next = { ...was, ...change }
    if (next.answer !== was.answer || next.asking !== was.asking) o.setSlots({ ...o.slots(), [id]: next })
  }
  function drop(id: string) {
    inFlight.get(id)?.abort()
    inFlight.delete(id)
    const timer = timers.get(id)
    if (timer !== undefined) forget(timer)
    timers.delete(id)
  }
  function took(id: string, answer: TileComment, moment: CommentMoment, tries: number) {
    const now = o.slots()[id]?.answer ?? null
    if (answer.state === 'pending') {
      patch(id, { answer: now?.text ? now : same(now, answer) ? now : answer, asking: moment === 'asked' ? moment : false })
      if (tries < PENDING_TRIES) {
        timers.set(id, later(() => {
          timers.delete(id)
          if (o.hidden()) due.set(id, strongest(due.get(id), moment))
          else send(id, moment, tries + 1)
        }, PENDING_PAUSE_MS))
      } else if (moment === 'asked') patch(id, { asking: false })
      return
    }
    patch(id, { answer: same(now, answer) ? now : answer, asking: false })
    if (moment === 'asked') o.wrote?.(id)
  }
  function send(id: string, moment: CommentMoment, tries = 0) {
    const controller = new AbortController()
    inFlight.set(id, controller)
    patch(id, { asking: moment })
    o.call(id, moment, controller.signal).then((answer) => {
      if (inFlight.get(id) !== controller) return
      inFlight.delete(id)
      took(id, answer, moment, tries)
    }, (e) => {
      if (inFlight.get(id) !== controller || isAbort(e)) return
      inFlight.delete(id)
      const now = o.slots()[id]?.answer ?? null
      patch(id, { answer: now?.text ? now : unanswered(o.failed()), asking: false })
      if (moment === 'asked') o.wrote?.(id)
    })
  }

  return {
    /** Asks these comments, now or when the tab is back in sight. */
    ask(ids: string[], moment: CommentMoment) {
      for (const id of ids) {
        if (moment === 'refresh' && (inFlight.has(id) || timers.has(id))) continue
        drop(id)
        if (o.hidden()) due.set(id, strongest(due.get(id), moment))
        else {
          due.delete(id)
          send(id, moment)
        }
      }
    },
    /** What is on screen no longer speaks of what is asked: a skeleton, at once. */
    blank(ids: string[]) {
      for (const id of ids) {
        drop(id)
        due.delete(id)
        patch(id, { answer: null, asking: false })
      }
    },
    /** The tab is back in sight. */
    resume() {
      const waiting = [...due]
      due.clear()
      for (const [id, moment] of waiting) {
        drop(id)
        send(id, moment)
      }
    },
    /** Nothing asked any more, and nothing to come. */
    stop() {
      for (const id of [...inFlight.keys(), ...timers.keys()]) drop(id)
      due.clear()
    },
  }
}

function strongest(a: CommentMoment | undefined, b: CommentMoment): CommentMoment {
  const rank = (m: CommentMoment | undefined) => (m === 'asked' ? 2 : m === 'open' ? 1 : 0)
  return rank(a) >= rank(b) ? a ?? b : b
}
