// The pages opened by a button or a row rather than by a link (the editor, a dashboard, a
// report, the Viewer…): their code is fetched once the app is idle after signing in, one
// page per idle moment, so the first click on a flow does not wait for the editor's code
// with the screen still. The links prefetch by themselves (nuxt.config.ts, prefetchOn).
const PAGES = [
  '/editor', '/viewer', '/dashboard', '/report', '/flow-group', '/sql-script',
  '/dashboards', '/reports', '/admin', '/queue', '/performance', '/audit',
  '/dashboard-design', '/report-design', '/cluster', '/scripting', '/monitoring',
]

export default defineNuxtPlugin((nuxtApp) => {
  const idle = (fn: () => void) =>
    'requestIdleCallback' in window ? window.requestIdleCallback(fn, { timeout: 5000 }) : setTimeout(fn, 1500)
  let started = false
  nuxtApp.hook('page:finish', () => {
    if (started || !useSessionPresent().value) return
    started = true
    const queue = [...PAGES]
    const next = () => {
      const path = queue.shift()
      if (!path) return
      preloadRouteComponents(path).catch(() => { /* fetched at the click, as before */ }).finally(() => idle(next))
    }
    idle(next)
  })
})
