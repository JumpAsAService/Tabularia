// Uscita per inattività (AUTH__SESSION_IDLE_MINUTES, che il gateway manda in
// /auth/me). Il gateway chiude da sé una sessione da cui non arrivano richieste,
// ma una pagina aperta ne fa anche senza nessuno davanti (la presenza nell'editor,
// l'avanzamento di un run): per questo il conto lo tiene anche la pagina, su
// quello che fa la PERSONA — tasti, mouse, rotella, tocchi.
//
// L'attività è condivisa fra le schede (localStorage): chi lavora in una non
// viene buttato fuori dall'altra. Un minuto prima l'avviso (IdleWarning.vue);
// allo scadere, uscita vera, come dal menu.
//
// Il rovescio: chi legge una tabella lunga senza fare richieste è attivo per la
// pagina ma fermo per il gateway. Allora, finché c'è attività, la pagina lo dice
// al gateway una volta al minuto.
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
      try { localStorage.setItem(KEY, String(last)) } catch { /* storage negato: vale la scheda */ }
    }
  }
  function lastActivity(): number {
    let shared = 0
    try { shared = Number(localStorage.getItem(KEY)) || 0 } catch { /* idem */ }
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
    // l'ultimo minuto, o l'ultima metà con un timeout più corto di due minuti
    warning.value = left <= Math.min(WARN_MS, (minutes * 60_000) / 2) ? Math.ceil(left / 1000) : null
    // attiva sulla pagina dall'ultimo segnale: il gateway lo deve sapere
    if (seen > pinged && now - pinged >= PING_MS) {
      pinged = now
      await nuxtApp.runWithContext(() => useAuth().fetchMe())
    }
  }

  for (const e of EVENTS) window.addEventListener(e, mark, { passive: true, capture: true })
  // all'accesso il conto riparte: l'attività di prima appartiene a un'altra sessione
  watch(() => user.value?.id, (id) => { if (id != null) { mark(); pinged = Date.now() } })
  setInterval(tick, 1_000)
})
