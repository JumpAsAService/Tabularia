// Sign-out for inactivity (AUTH__SESSION_IDLE_MINUTES, which the gateway sends in
// /auth/me). The gateway closes by itself a session no requests come from, but an open
// page makes some even with nobody in front of it (the presence in the editor, a run's
// progress): that is why the page keeps the count too, on what the PERSON does — keys,
// mouse, wheel, touches.
//
// The activity is shared among the tabs (localStorage): whoever works in one is not
// thrown out of the other. A minute before, the warning (IdleWarning.vue); at expiry, a
// real sign-out, as from the menu.
//
// The flip side: whoever reads a long table without making requests is active for the
// page but still for the gateway. So, while there is activity, the page tells the
// gateway once a minute.
import type { Me } from '~/composables/useAuth'

const KEY = 'tabularia.lastActivity'
const EVENTS = ['pointerdown', 'keydown', 'wheel', 'touchstart', 'mousemove', 'scroll'] as const
const WARN_MS = 60_000
const PING_MS = 60_000

export default defineNuxtPlugin((nuxtApp) => {
  const user = useState<Me | null>('auth_user', () => null)
  const present = useSessionPresent()
  const warning = useIdleWarning()
  let last = Date.now()
  let written = 0
  let pinged = Date.now()
  let leaving = false

  function mark() {
    last = Date.now()
    if (last - written > 5_000) {
      written = last
      try { localStorage.setItem(KEY, String(last)) } catch { /* storage denied: the tab's holds */ }
    }
  }
  function lastActivity(): number {
    let shared = 0
    try { shared = Number(localStorage.getItem(KEY)) || 0 } catch { /* same */ }
    return Math.max(last, shared)
  }

  async function tick() {
    const minutes = user.value?.session_idle_minutes ?? 0
    if (!user.value || !present.value || minutes <= 0) {
      warning.value = null
      leaving = false
      return
    }
    const now = Date.now()
    const seen = lastActivity()
    const left = minutes * 60_000 - (now - seen)
    if (left <= 0) {
      if (leaving) return
      leaving = true
      warning.value = null
      await nuxtApp.runWithContext(() => useAuth().logout('idle'))
      return
    }
    // the last minute, or the last half with a timeout shorter than two minutes
    warning.value = left <= Math.min(WARN_MS, (minutes * 60_000) / 2) ? Math.ceil(left / 1000) : null
    // active on the page since the last signal: the gateway must know
    if (seen > pinged && now - pinged >= PING_MS) {
      pinged = now
      await nuxtApp.runWithContext(() => useAuth().fetchMe())
    }
  }

  for (const e of EVENTS) window.addEventListener(e, mark, { passive: true, capture: true })
  // at sign-in the count starts again: the activity of before belongs to another session
  watch(() => user.value?.id, (id) => { if (id != null) { mark(); pinged = Date.now() } })
  setInterval(tick, 1_000)
})
