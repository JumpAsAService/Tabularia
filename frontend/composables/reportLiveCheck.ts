// The server's check of a report while it is being edited (POST /reports/check,
// docs/specs/reports.md): after a pause in the editing the whole definition goes to the
// gateway, which answers with every problem it and the engine see — types, scopes, the
// fields of the datasets, the formats — as the save would. The editor's own check
// (reportExpressions.ts) answers at once; this one comes after it and says more.
//
// An answer is used only if the definition is still the one that was sent: what came back
// for an older text is dropped, and a newer pause cancels the call under way. A gateway
// without the route (404, 405), or a session that has ended (401: the page says so once,
// not at every pause in the typing), is not asked again for the life of the page; one that
// is slow, fails or cannot be reached leaves the editor's check alone, with nothing said:
// the save checks everything all the same. No timers or fetches of its own, so it can be
// tested with fakes.

export interface LiveCheckOptions {
  /** sends the definition; resolves to the gateway's answer, rejects as $fetch does */
  call: (definition: unknown, signal: AbortSignal) => Promise<unknown>
  /** what the definition is now, as text: an answer for another text is stale */
  current: () => string
  /** the problems of the definition as it is (the raw list of the answer) */
  onProblems: (problems: unknown[], text: string) => void
  /** the server cannot say anything now: what it said before no longer holds */
  onClear: () => void
  /** the pause after the last change, in ms */
  delay?: number
  /** how long an answer is waited for, in ms */
  timeout?: number
  timers?: { set: (fn: () => void, ms: number) => unknown; clear: (handle: unknown) => void }
}

export interface LiveCheck {
  /** the definition changed (its text): checked after the pause; `build` makes the body */
  schedule: (text: string, build: () => unknown) => void
  /** nothing more to check (the page goes away): the timer and the call stop */
  stop: () => void
  /** false once the gateway has said it has no such route */
  readonly available: boolean
}

/** The status of a failed $fetch (or of anything shaped like it). */
function statusOf(e: any): number | null {
  const s = e?.statusCode ?? e?.status ?? e?.response?.status
  return typeof s === 'number' ? s : null
}

export function createLiveCheck(o: LiveCheckOptions): LiveCheck {
  const delay = o.delay ?? 600
  const timeout = o.timeout ?? 8000
  const timers = o.timers ?? {
    set: (fn: () => void, ms: number) => setTimeout(fn, ms),
    clear: (h: unknown) => clearTimeout(h as ReturnType<typeof setTimeout>),
  }
  let available = true
  let pause: unknown = null
  let controller: AbortController | null = null
  let watchdog: unknown = null
  /** the text the last answer was about: the same text is not sent again */
  let checked: string | null = null

  function cancel() {
    if (watchdog != null) { timers.clear(watchdog); watchdog = null }
    controller?.abort()
    controller = null
  }

  async function send(text: string, build: () => unknown) {
    pause = null
    if (!available || text === checked || text !== o.current()) return
    cancel()
    const mine = new AbortController()
    controller = mine
    watchdog = timers.set(() => mine.abort(), timeout)
    let answer: unknown
    try {
      answer = await o.call(build(), mine.signal)
    } catch (e) {
      if (controller !== mine) return // a newer call took its place
      cancel()
      const status = statusOf(e)
      if (status === 401 || status === 404 || status === 405) available = false
      checked = null
      o.onClear()
      return
    }
    if (controller !== mine) return
    cancel()
    // the definition changed while the server was reading it: the next pause asks again
    if (text !== o.current()) return
    const list = (answer as { problems?: unknown })?.problems
    checked = text
    o.onProblems(Array.isArray(list) ? list : [], text)
  }

  return {
    schedule(text, build) {
      if (!available) return
      if (pause != null) timers.clear(pause)
      pause = timers.set(() => { void send(text, build) }, delay)
    },
    stop() {
      if (pause != null) { timers.clear(pause); pause = null }
      cancel()
    },
    get available() { return available },
  }
}
