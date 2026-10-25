// Light/dark theme. The theme is a `data-theme` attribute on <html>; the whole palette
// lives in CSS variables (assets/main.css), so changing theme = flipping the attribute.
// DARK by default at the first visit; the choice is persisted in localStorage and read
// again cold by the anti-flash script in nuxt.config.
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

// state shared at module level: one theme for the whole app
const theme = ref<Theme>('dark')
let initialized = false

function apply(t: Theme) {
  if (!import.meta.client) return
  document.documentElement.setAttribute('data-theme', t)
  try { localStorage.setItem(STORAGE_KEY, t) } catch { /* storage denied: it holds until the reload */ }
}

export function useTheme() {
  if (!initialized && import.meta.client) {
    initialized = true
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && VALID.has(saved)) theme.value = saved as Theme
    apply(theme.value)
  }

  // The theme is applied and saved HERE, the moment it is chosen. Before, a `watch` created
  // by the first component calling useTheme did it: it died with it, and every page has its
  // own bar. After a page change the selector changed the value but not the attribute, and
  // a reload was needed (2026-10-12).
  function setTheme(t: Theme) {
    if (!VALID.has(t)) return
    theme.value = t
    apply(t)
  }
  function toggle() {
    // quick dark↔light toggle (the extra themes are chosen from the picker)
    setTheme(theme.value === 'light' ? 'dark' : 'light')
  }

  return { theme, setTheme, toggle, themes: THEMES }
}
