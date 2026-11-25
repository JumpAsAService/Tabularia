// The page's entrance: when the router lands on another page, its content fades in for a moment
// (assets/main.css, «page entrance»). Only a CSS animation on the content, started once the new
// page is rendered: nothing is held back, no picture of the old page is kept, no page is mounted
// twice. Which navigations count is composables/pageTransition.ts.
//
// It looks at router.afterEach with no failure: a navigation the person cancelled (the leave
// question answered Cancel) or that another one replaced never gets there, so nothing starts.
import { shouldAnimate } from '~/composables/pageTransition'

/** The attribute stays this long (ms): the animation is shorter, this only clears it. */
const HOLD = 300

export default defineNuxtPlugin((nuxtApp) => {
  const router = useRouter()
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
  const root = document.documentElement
  let pending = false
  let timer: ReturnType<typeof setTimeout> | undefined

  router.afterEach((to, from, failure) => {
    pending = !failure && shouldAnimate(from, to, reduced.matches)
  })
  // the new page is in the DOM (its code and its setup done): the fade starts now
  nuxtApp.hook('page:finish', () => {
    if (!pending) return
    pending = false
    clearTimeout(timer)
    root.removeAttribute('data-page-enter')
    void root.offsetWidth // a second change in a row restarts the animation
    root.setAttribute('data-page-enter', '')
    timer = setTimeout(() => root.removeAttribute('data-page-enter'), HOLD)
  })
})
