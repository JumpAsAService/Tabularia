// Surface finish: a material (carbon, titanium, marble, slate, glass, aurora, glow, grain, honeycomb, brutal, retro) laid over the colour theme,
// plus how strong its highlights are. On <html> it is a `data-finish` attribute (ABSENT for
// «plain», the app as it is) and an inline `--finish-sheen` number from 0 to 1; the styles are in
// assets/finishes.css. Both choices are persisted in localStorage and read again cold by the
// anti-flash script in nuxt.config, which must use the same keys and values.
import { ref } from 'vue'

export type Finish = 'plain' | 'carbon' | 'titanium' | 'marble' | 'slate' | 'glass' | 'aurora' | 'glow' | 'grain' | 'honeycomb' | 'brutal' | 'retro'
export const FINISHES: Finish[] = ['plain', 'carbon', 'titanium', 'marble', 'slate', 'glass', 'aurora', 'glow', 'grain', 'honeycomb', 'brutal', 'retro']
export const FINISH_STORAGE_KEY = 'tabularia-finish'
export const SHEEN_STORAGE_KEY = 'tabularia-sheen'
export const SHEEN_DEFAULT = 50
export const SHEEN_STEP = 5

/** A saved or chosen finish, or 'plain' when it is not a known one. */
export function finishOf(v: unknown): Finish {
  return typeof v === 'string' && (FINISHES as string[]).includes(v) ? (v as Finish) : 'plain'
}

/** A saved or chosen sheen as an integer 0–100 on the step; anything not a number gives the default. */
export function sheenOf(v: unknown): number {
  const n = typeof v === 'string' && v.trim() !== '' ? Number(v) : v
  if (typeof n !== 'number' || !Number.isFinite(n)) return SHEEN_DEFAULT
  return Math.min(100, Math.max(0, Math.round(n / SHEEN_STEP) * SHEEN_STEP))
}

const finish = ref<Finish>('plain')
const sheen = ref<number>(SHEEN_DEFAULT)
let initialized = false

function applyFinish(f: Finish) {
  if (!import.meta.client) return
  const root = document.documentElement
  if (f === 'plain') root.removeAttribute('data-finish')
  else root.setAttribute('data-finish', f)
  try { localStorage.setItem(FINISH_STORAGE_KEY, f) } catch { /* storage denied: it holds until the reload */ }
}

function applySheen(n: number) {
  if (!import.meta.client) return
  document.documentElement.style.setProperty('--finish-sheen', String(n / 100))
  try { localStorage.setItem(SHEEN_STORAGE_KEY, String(n)) } catch { /* storage denied */ }
}

export function useFinish() {
  if (!initialized && import.meta.client) {
    initialized = true
    try {
      finish.value = finishOf(localStorage.getItem(FINISH_STORAGE_KEY))
      sheen.value = sheenOf(localStorage.getItem(SHEEN_STORAGE_KEY))
    } catch { /* storage denied: the defaults */ }
    applyFinish(finish.value)
    applySheen(sheen.value)
  }

  // Applied and saved HERE, the moment it is chosen (same reason as useTheme: no watch).
  function setFinish(f: Finish) {
    finish.value = finishOf(f)
    applyFinish(finish.value)
  }
  function setSheen(n: number) {
    sheen.value = sheenOf(n)
    applySheen(sheen.value)
  }

  return { finish, sheen, setFinish, setSheen, finishes: FINISHES }
}
