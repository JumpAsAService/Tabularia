// Tema chiaro/scuro. Il tema è un attributo `data-theme` su <html>; tutta la
// palette vive in CSS variables (assets/main.css), quindi cambiare tema = flip
// dell'attributo. Default SCURO al primo accesso; la scelta è persistita in
// localStorage e riletta a freddo dallo script anti-flash in nuxt.config.
import { ref } from 'vue'

export type Theme = 'dark' | 'light' | 'dracula' | 'monokai' | 'imperium'
export const THEMES: { value: Theme; label: string }[] = [
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
  { value: 'dracula', label: 'Dracula' },
  { value: 'monokai', label: 'Monokai' },
  { value: 'imperium', label: 'Imperium' },
]
const VALID = new Set<string>(THEMES.map((t) => t.value))
const STORAGE_KEY = 'tabularia-theme'

// stato condiviso a livello di modulo: un solo tema per tutta l'app
const theme = ref<Theme>('dark')
let initialized = false

function apply(t: Theme) {
  if (!import.meta.client) return
  document.documentElement.setAttribute('data-theme', t)
  try { localStorage.setItem(STORAGE_KEY, t) } catch { /* storage negato: vale fino al ricaricamento */ }
}

export function useTheme() {
  if (!initialized && import.meta.client) {
    initialized = true
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && VALID.has(saved)) theme.value = saved as Theme
    apply(theme.value)
  }

  // Il tema si applica e si salva QUI, nel momento in cui lo si sceglie. Prima lo
  // faceva un `watch` creato dal primo componente che chiamava useTheme: moriva
  // con lui, e ogni pagina ha la sua barra. Dopo un cambio di pagina il selettore
  // cambiava il valore ma non l'attributo, e serviva ricaricare (2026-10-12).
  function setTheme(t: Theme) {
    if (!VALID.has(t)) return
    theme.value = t
    apply(t)
  }
  function toggle() {
    // toggle rapido scuro↔chiaro (i temi extra si scelgono dal picker)
    setTheme(theme.value === 'light' ? 'dark' : 'light')
  }

  return { theme, setTheme, toggle, themes: THEMES }
}
