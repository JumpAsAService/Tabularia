// Tema chiaro/scuro. Il tema è un attributo `data-theme` su <html>; tutta la
// palette vive in CSS variables (assets/main.css), quindi cambiare tema = flip
// dell'attributo. Default SCURO al primo accesso; la scelta è persistita in
// localStorage e riletta a freddo dallo script anti-flash in nuxt.config.
import { ref, watch } from 'vue'

export type Theme = 'dark' | 'light'
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
    if (saved === 'light' || saved === 'dark') theme.value = saved
    apply(theme.value)
    watch(theme, (t) => {
      localStorage.setItem(STORAGE_KEY, t)
      apply(t)
    })
  }

  function setTheme(t: Theme) {
    theme.value = t
  }
  function toggle() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  }

  return { theme, setTheme, toggle }
}
