// Page transitions: a short cross-fade of the page's content when the router changes page, and
// the opened object's name (a flow, a report… clicked in a list) carried into the title of its
// page. The decision and the names are composables/pageTransition.ts; the look is in
// assets/main.css («page transitions»).
//
// Why the View Transitions API and not <NuxtPage :transition>: Vue's own transition keeps the
// old page and the new one mounted side by side while it runs — two flow editors (Vue Flow,
// Monaco) at once, both readable by a screen reader, and the incoming page laid out under the
// outgoing one. The browser's transition takes pictures of the old page, swaps the DOM and
// fades the pictures, so there is one page, mounted once, all the time. Where the API is
// missing (Firefox before 144, older browsers) or reduced motion is asked, nothing is
// registered or nothing animates, and the page changes as it always did.
//
// It starts in router.beforeResolve: the last guard, after the pages' onBeforeRouteLeave
// (the confirmation dialog included) and the route middleware have agreed. A navigation the
// person cancels never gets here, so no transition starts and none is left half way.
import {
  OPENED_TTL, OPEN_SELECTOR, TITLE_SELECTOR, TITLE_WAIT, createObjectNames, shouldAnimate,
} from '~/composables/pageTransition'

/** The longest a transition may hold the page waiting for the next one to be ready (ms). */
const SAFETY = 3000

export default defineNuxtPlugin((nuxtApp) => {
  const doc = document as Document & {
    startViewTransition?: (update: () => Promise<void> | void) => { finished: Promise<unknown> }
  }
  if (typeof doc.startViewTransition !== 'function') return

  const root = document.documentElement
  const router = useRouter()
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
  const names = createObjectNames()

  // the row that was opened (a click, or Enter on a focused row), until the navigation asks
  let opened: { el: HTMLElement; at: number } | null = null
  const rowName = (target: EventTarget | null): HTMLElement | null => {
    if (!(target instanceof Element)) return null
    return (target.closest(OPEN_SELECTOR) ?? target.closest('tr[data-key]')?.querySelector(OPEN_SELECTOR) ?? null) as HTMLElement | null
  }
  document.addEventListener('click', (e) => {
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return // a new tab, not this page
    const el = rowName(e.target)
    opened = el ? { el, at: performance.now() } : null
  }, true)
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return
    const el = rowName(e.target)
    if (el) opened = { el, at: performance.now() }
  }, true)

  // the browser's own swipe animation (Safari) already did the job for a back gesture
  let browserAnimated = false
  window.addEventListener('popstate', (e) => { browserAnimated = !!(e as PopStateEvent & { hasUAVisualTransition?: boolean }).hasUAVisualTransition })

  let seq = 0
  let finish: (() => void) | null = null

  // the page that has just opened: waits a moment for its title (its data is on the way), gives
  // it the opened object's name, then lets the browser take the picture of the new page
  async function settle() {
    const done = finish
    if (!done) return
    finish = null
    if (names.current()) {
      let title = document.querySelector<HTMLElement>(TITLE_SELECTOR)
      if (!title) {
        title = await new Promise<HTMLElement | null>((resolve) => {
          const stop = (el: HTMLElement | null) => { obs.disconnect(); clearTimeout(timer); resolve(el) }
          const obs = new MutationObserver(() => {
            const el = document.querySelector<HTMLElement>(TITLE_SELECTOR)
            if (el) stop(el)
          })
          obs.observe(document.body, { childList: true, subtree: true })
          const timer = setTimeout(() => stop(null), TITLE_WAIT)
        })
      }
      names.claim(title)
    }
    done()
  }
  nuxtApp.hook('page:finish', settle)
  nuxtApp.hook('app:error', () => { const d = finish; finish = null; d?.() })
  nuxtApp.hook('vue:error', () => { const d = finish; finish = null; d?.() })
  // a navigation that did not end in the new page (cancelled, replaced by another, failed)
  router.afterEach((_to, _from, failure) => { if (failure) { const d = finish; finish = null; d?.() } })

  router.beforeResolve(async (to, from) => {
    const animate = shouldAnimate(from, to, { supported: true, reducedMotion: reduced.matches })
    if (!animate || browserAnimated) { browserAnimated = false; return }

    const mine = ++seq
    const stale = !opened || performance.now() - opened.at > OPENED_TTL || !opened.el.isConnected
    names.begin(stale ? null : opened!.el)
    opened = null
    root.classList.add('vt-active')

    const next = new Promise<void>((resolve) => { finish = resolve })
    let swap!: () => void
    const swapped = new Promise<void>((resolve) => { swap = resolve })
    const safety = setTimeout(() => { const d = finish; finish = null; d?.() }, SAFETY)

    const transition = doc.startViewTransition!(() => { swap(); return next })
    const cleanup = () => {
      clearTimeout(safety)
      // a newer transition owns the class and the names now
      if (seq !== mine) return
      root.classList.remove('vt-active')
      names.release()
    }
    transition.finished.then(cleanup, cleanup)
    // the router goes on once the old page's picture is taken (the next frame), not before
    await Promise.race([swapped, new Promise<void>((resolve) => setTimeout(resolve, 1000))])
  })
})
