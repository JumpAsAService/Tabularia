// The logic of the page transitions (plugins/page-transition.client.ts), kept apart from the
// browser so a test can run it: which navigations animate, and the names that carry an opened
// object (a flow, a report…) from its row in a list to the title of its page.
//
// A transition is the View Transitions API's: the browser takes a picture of the page, the
// router swaps it for the next one, and the pictures cross-fade (assets/main.css, «page
// transitions»). The pictures are all that is doubled: the page itself is mounted once.

/** What the decision needs of a route (a vue-router location, or anything shaped like one). */
export interface RouteLike {
  path: string
  matched: ReadonlyArray<{ path: string; components?: unknown } | undefined>
  meta?: { viewTransition?: unknown } | Record<string, unknown>
}

export interface TransitionContext {
  /** document.startViewTransition exists (Chrome, Safari, Firefox 144 and later) */
  supported: boolean
  /** the person asked for less motion: the page then changes with no animation at all */
  reducedMotion: boolean
}

/** The page a route shows: the last record it matched, that is the page's own file. */
function pageOf(route: RouteLike): string | null {
  const record = route.matched[route.matched.length - 1]
  return record ? record.path : null
}

/**
 * Whether going `from` → `to` animates. Not when the browser lacks the API or reduced motion
 * is asked, not for the first page of a visit, not when the page is the same one (a query, a
 * hash or a parameter that changes what it shows: a filter, a folder, another flow in the
 * editor), and not when a page opts out with `definePageMeta({ viewTransition: false })`.
 */
export function shouldAnimate(from: RouteLike, to: RouteLike, ctx: TransitionContext): boolean {
  if (!ctx.supported || ctx.reducedMotion) return false
  const next = pageOf(to)
  const prev = pageOf(from)
  if (next === null || prev === null) return false // unknown address, or the first page
  if (next === prev) return false
  if (to.meta?.viewTransition === false || from.meta?.viewTransition === false) return false
  return true
}

/** The part of an element the names need: its inline style. */
export interface Nameable {
  style: { viewTransitionName: string }
}

/**
 * The object opened from a list: its row's name is given a transition name, and so is the
 * title of the page that opens, so the browser moves one into the other. One name per
 * transition (a counter), set on at most two elements, and taken off when the transition
 * is over, so no name outlives it and two names never meet on the page.
 */
export function createObjectNames() {
  let counter = 0
  let active: { name: string; elements: Nameable[] } | null = null

  function release() {
    if (!active) return
    for (const el of active.elements) {
      // only our own name: an element that has taken another one in the meantime keeps it
      if (el.style.viewTransitionName === active.name) el.style.viewTransitionName = ''
    }
    active = null
  }

  return {
    /** Starts a transition's names with the row that was clicked; null when there was none. */
    begin(source: Nameable | null): string | null {
      release()
      if (!source) return null
      const name = `vt-open-${++counter}`
      source.style.viewTransitionName = name
      active = { name, elements: [source] }
      return name
    },
    /** The title of the page that opened: takes the name when a transition has one waiting. */
    claim(title: Nameable | null): boolean {
      if (!active || !title || active.elements.includes(title)) return false
      if (active.elements.length > 1) return false // one title only
      title.style.viewTransitionName = active.name
      active.elements.push(title)
      return true
    },
    /** The name in use, if any (for the tests and the log). */
    current(): string | null {
      return active?.name ?? null
    },
    release,
  }
}

/** How long a click on a row stays the answer to «which object was opened» (ms). */
export const OPENED_TTL = 1500

/** The longest the swap waits for the opened page's title to show (ms): its data is on its way. */
export const TITLE_WAIT = 100

/** Selectors of the markers: a row's name (in a list) and the title of a page. */
export const OPEN_SELECTOR = '[data-vt-open]'
export const TITLE_SELECTOR = '[data-vt-title]'
