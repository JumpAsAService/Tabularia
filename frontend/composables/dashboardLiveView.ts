// The designer's numbers (docs/specs/dashboards.md, «On the screen»): the data is on the canvas, so a
// change of a tile asks POST /dashboards/preview for that tile, and only for it. No timers here: the
// page calls `run` after a pause in the editing, and this one decides what that asks, so it can be
// tested with fakes. `slots` are the viewer's (dashboardTiles.ts): a tile listens to the latest request
// that asked for it, so an old answer arriving late changes nothing there, and a request none of its
// tiles waits for any more is dropped.
//
// The server refuses a definition as a save would, whole. A tile half made is not sent: it says
// what it needs by itself (`ready`). A tile it finds a mistake in says it inside the tile and is left
// out, and the others go on. A problem of the dashboard's own (a dataset, a parameter, the
// interval) stops every tile: it is said once (`say`), and the numbers on screen stay.
import { previewSignatures, tilesChanged, withoutTiles } from '~/composables/dashboardDesignModel'
import { beginAsking, emptySlots, tookAnswer, tookFailure, type TileSlots } from '~/composables/dashboardTiles'
import type { DesignProblem } from '~/composables/reportDesignModel'
import type { DashboardDefinition, TileAnswer, TilesAnswer } from '~/composables/useDashboards'

export interface LiveViewOptions {
  /** the definition as it is now, images as a pixel */
  build: () => DashboardDefinition
  /** the parameters' values the tiles are read with */
  values: () => Record<string, unknown>
  slots: () => TileSlots
  setSlots: (s: TileSlots) => void
  /** whether a tile says enough to be asked */
  ready: (tile: Record<string, any>) => boolean
  /** what a tile's answer is without asking (an image, drawn from the definition) */
  local: (tile: Record<string, any>) => TileAnswer | null
  /** the problem of the whole dashboard; empty when there is none */
  say: (message: string) => void
  /** rejects as $fetch does; a refusal with `problems`, already mapped, lands on its tiles */
  call: (definition: DashboardDefinition, ids: string[], values: Record<string, unknown>, signal: AbortSignal) => Promise<TilesAnswer>
  errorOf: (e: unknown) => string
}

export interface LiveView {
  /** `problems`: what the server's check found in the definition as it is (undefined: it could not
   *  say). `again`: every tile, its numbers read again. */
  run: (problems?: DesignProblem[], how?: { again?: boolean }) => void
  stop: () => void
}

const isAbort = (e: any) => e?.name === 'AbortError' || e?.cause?.name === 'AbortError'

export function createLiveView(o: LiveViewOptions): LiveView {
  let seq = 0
  /** what each tile's numbers answer, as last asked */
  let asked: Record<string, string> = {}
  /** the tiles the server found a mistake in, by what it found: they stay out until they change */
  let refused = new Map<string, string>()
  const inFlight = new Map<number, { ids: string[]; controller: AbortController }>()

  function run(problems?: DesignProblem[], how: { again?: boolean } = {}) {
    if (problems) {
      refused = new Map()
      for (const p of problems) if (p.item && !refused.has(p.item)) refused.set(p.item, p.message)
    }
    const whole = problems?.find((p) => !p.item)
    o.say(whole?.message ?? '')
    if (whole) return
    const full = o.build()
    const tiles = full.tiles

    // the slots of the tiles there are: one taken away has none, and one that comes back starts over
    const current = o.slots()
    let slots: TileSlots = Object.fromEntries(tiles.map((t) => [t.id, current[t.id] ?? emptySlots([t.id])[t.id]]))
    const gone = new Set(tiles.map((t) => t.id))
    asked = Object.fromEntries(Object.entries(asked).filter(([id]) => gone.has(id)))

    // what is not asked: half made, refused, or known here
    const out = new Set<string>()
    const known = new Set<string>()
    for (const t of tiles) {
      const made = o.ready(t)
      const mistake = refused.get(t.id)
      const here = made && !mistake ? o.local(t) : null
      if (made && !mistake && !here) continue
      delete asked[t.id]
      if (here) known.add(t.id)
      else out.add(t.id)
      if (mistake) {
        const was = slots[t.id].answer
        if (!(was && 'error' in was && was.error === mistake)) slots[t.id] = { answer: { error: mistake, code: 'error' }, asked: 0, waiting: false, trouble: null }
      } else {
        slots[t.id] = here ? { answer: here, asked: 0, waiting: false, trouble: null } : emptySlots([t.id])[t.id]
      }
    }
    const definition = withoutTiles(full, out)
    const values = o.values()
    const signatures = previewSignatures(definition, values)
    for (const id of known) delete signatures[id]
    const ids = how.again ? Object.keys(signatures) : tilesChanged(asked, signatures)
    if (!ids.length) {
      o.setSlots(slots)
      return
    }
    const mine = ++seq
    slots = beginAsking(slots, ids, mine, !how.again)
    o.setSlots(slots)
    for (const [other, request] of inFlight) {
      if (request.ids.every((tile) => slots[tile]?.asked !== other)) {
        request.controller.abort()
        inFlight.delete(other)
      }
    }
    for (const id of ids) asked[id] = signatures[id]
    const controller = new AbortController()
    inFlight.set(mine, { ids, controller })
    o.call(definition, ids, values, controller.signal).then((answer) => {
      if (controller.signal.aborted) return
      o.setSlots(tookAnswer(o.slots(), mine, answer.tiles, { keep: false }))
    }, (e: any) => {
      if (controller.signal.aborted || isAbort(e)) return
      if (Array.isArray(e?.problems) && e.problems.length) {
        for (const id of ids) if (o.slots()[id]?.asked === mine) delete asked[id]
        run(e.problems)
        return
      }
      for (const id of ids) if (o.slots()[id]?.asked === mine) delete asked[id]
      o.setSlots(tookFailure(o.slots(), mine, o.errorOf(e), { keep: false }))
    }).finally(() => { inFlight.delete(mine) })
  }

  return {
    run,
    stop() {
      for (const { controller } of inFlight.values()) controller.abort()
      inFlight.clear()
    },
  }
}
