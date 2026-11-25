// Which navigations get the page's entrance (plugins/page-enter.client.ts), kept apart from the
// browser so a test can run it.

/** What the decision needs of a route (a vue-router location, or anything shaped like one). */
export interface RouteLike {
  path: string
  matched: ReadonlyArray<{ path: string } | undefined>
  meta?: { pageEnter?: unknown } | Record<string, unknown>
}

/** The page a route shows: the last record it matched, that is the page's own file. */
function pageOf(route: RouteLike): string | null {
  const record = route.matched[route.matched.length - 1]
  return record ? record.path : null
}

/**
 * Whether going `from` → `to` brings the new page in with the fade. Not with reduced motion,
 * not for the first page of a visit, not when the page is the same one (a query, a hash or a
 * parameter that only changes what it shows: a filter, a folder, another flow in the editor), and
 * not when a page opts out with `definePageMeta({ pageEnter: false })`.
 */
export function shouldAnimate(from: RouteLike, to: RouteLike, reducedMotion: boolean): boolean {
  if (reducedMotion) return false
  const next = pageOf(to)
  const prev = pageOf(from)
  if (next === null || prev === null) return false // unknown address, or the first page
  if (next === prev) return false
  if (to.meta?.pageEnter === false || from.meta?.pageEnter === false) return false
  return true
}
