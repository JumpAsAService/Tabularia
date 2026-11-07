// The footer's privacy and cookie policy links open in iubenda's lightbox.
// iubenda.js wires the links it finds when it runs, so it runs only once the page is
// hydrated: loaded with the page it raced Vue's hydration (a mismatch, and only the
// first link wired). After a client-side navigation it runs again (from the browser's
// cache) to wire the new footer; the links it already wired it skips.
// Without the script the links are plain links to the policy pages. It sets no cookies.
const SRC = 'https://cdn.iubenda.com/iubenda.js'

export default defineNuxtPlugin((nuxtApp) => {
  let ready = false
  const run = () => {
    document.getElementById('iubenda-js')?.remove()
    const s = document.createElement('script')
    s.id = 'iubenda-js'
    s.src = SRC
    s.async = true
    document.body.appendChild(s)
  }
  onNuxtReady(() => { ready = true; run() })
  nuxtApp.hook('page:finish', () => { if (ready) setTimeout(run, 0) })
})
