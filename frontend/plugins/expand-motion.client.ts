// Every `<details>` of the app opens and closes over a moment (composables/expandMotion.ts)
// instead of at once: one listener on the document, so no component has anything to do. The
// keyboard goes the same way (Enter and Space on a summary are a click). With reduced motion
// nothing is touched and the browser opens and closes as it always did; so does what opens a box
// without a click (a search that finds text inside it, the state of the page).
import { expanderOf, toggleExpand } from '~/composables/expandMotion'

export default defineNuxtPlugin(() => {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
  document.addEventListener('click', (event) => {
    if (reduced.matches) return
    const hit = expanderOf(event)
    if (!hit) return
    event.preventDefault() // the browser's own toggle, which is the one at once
    const { details, summary } = hit
    void toggleExpand(details, summary, details.open, (open) => { details.open = open })
  })
})
