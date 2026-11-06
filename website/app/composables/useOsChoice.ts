// The system the docs' Windows / Linux / macOS tabs show, shared by every tab group of the
// site. The head script in nuxt.config.ts sets <html data-os> before first paint (the
// visitor's last choice, else the browser's system); a choice is kept in localStorage.
export type OsSystem = 'windows' | 'linux' | 'macos'

const KEY = 'tabularia-site-os'
const SYSTEMS: OsSystem[] = ['windows', 'linux', 'macos']
const chosen = ref<OsSystem | null>(null)
let listening = false

const valid = (v: unknown): v is OsSystem => SYSTEMS.includes(v as OsSystem)

/** The systems a panel covers: "linux-macos" → linux, macos. */
export const panelSystems = (key: string): OsSystem[] => key.split('-').filter(valid)

/** The panel of a group that answers a system: its own, else the other Unix one, else the first. */
export function panelFor (keys: string[], os: OsSystem | null): string {
  if (!keys.length) return ''
  const has = (s: OsSystem) => keys.find(k => panelSystems(k).includes(s))
  if (os) {
    const own = has(os)
    if (own) return own
    const near = os === 'macos' ? has('linux') : os === 'linux' ? has('macos') : undefined
    if (near) return near
  }
  return keys[0]!
}

export function useOsChoice () {
  if (import.meta.client && chosen.value === null) {
    const v = document.documentElement.dataset.os
    chosen.value = valid(v) ? v : 'linux'
    if (!listening) {
      listening = true
      // another tab of the site changed it
      window.addEventListener('storage', (e) => {
        if (e.key === KEY && valid(e.newValue)) { chosen.value = e.newValue; document.documentElement.dataset.os = e.newValue }
      })
    }
  }
  /** Shows a panel everywhere: its system becomes the visitor's choice. */
  function choose (key: string) {
    const systems = panelSystems(key)
    if (!systems.length) return
    // "Linux and macOS" keeps a choice of either; otherwise it means Linux
    const os = chosen.value && systems.includes(chosen.value) ? chosen.value : systems[0]!
    chosen.value = os
    document.documentElement.dataset.os = os
    try { localStorage.setItem(KEY, os) } catch { /* private window, blocked storage: this page only */ }
  }
  return { chosen: readonly(chosen), choose }
}
