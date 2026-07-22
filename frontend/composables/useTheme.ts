// Tema chiaro/scuro. Il tema è un attributo `data-theme` su <html>; tutta la
// palette vive in CSS variables (assets/main.css), quindi cambiare tema = flip
// dell'attributo. Default SCURO al primo accesso; la scelta è persistita in
// localStorage e riletta a freddo dallo script anti-flash in nuxt.config.
import { ref, watch } from 'vue'

export type Theme = 'dark' | 'light' | 'dracula' | 'monokai'
export const THEMES: { value: Theme; label: string }[] = [
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
  { value: 'dracula', label: 'Dracula' },
  { value: 'monokai', label: 'Monokai' },
]
const VALID = new Set<string>(THEMES.map((t) => t.value))
const STORAGE_KEY = 'tabularia-theme'

// stato condiviso a livello di modulo: un solo tema per tutta l'app
const theme = ref<Theme>('dark')
let initialized = false

function apply(t: Theme) {
  if (import.meta.client) document.documentElement.setAttribute('data-theme', t)
}

export function useTheme() {
  if (!initialized && import.meta.client) {
    initialized = true
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && VALID.has(saved)) theme.value = saved as Theme
    apply(theme.value)
    watch(theme, (t) => {
      localStorage.setItem(STORAGE_KEY, t)
      apply(t)
    })
  }

  function setTheme(t: Theme) {
    if (VALID.has(t)) theme.value = t
  }
  function toggle() {
    // toggle rapido scuro↔chiaro (i temi extra si scelgono dal picker)
    theme.value = theme.value === 'light' ? 'dark' : 'light'
  }

  return { theme, setTheme, toggle, themes: THEMES }
}
