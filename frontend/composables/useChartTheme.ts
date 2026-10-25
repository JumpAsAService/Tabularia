// The charts' "chrome" colours (axes, labels, borders, tooltip background) read from the
// current theme's CSS tokens.
//
// ECharts does not read CSS variables: every option wants a colour resolved already. The
// same `readUi()` function was copied in three components (audit, ScheduleLoad,
// ChartPanel) and was missing in the other two (RunCalendar, RunGantt), which for this
// stayed tuned on the dark theme whatever theme was active. Here it sits once only, with
// the superset of the tokens the five use.
//
// The DATA colours (ramps, series) stay in the components: they are encoding, not chrome.
import { ref, watch } from 'vue'
import { useTheme } from '~/composables/useTheme'

export interface ChartUi {
  text: string
  muted: string
  border: string
  borderSoft: string
  panel: string
  panel2: string
  /** --accent-2: positive outcome */
  success: string
  /** --danger: negative outcome */
  danger: string
  /** --accent: running / selection */
  accent: string
  /** --violet: third category (e.g. runs, refreshes) */
  violet: string
  /** the theme has a light background (light, imperium): its `color-scheme` says so */
  isLight: boolean
}

// used by the server-side render, where `document` does not exist: they are the dark
// theme's values, which is the default without `data-theme`
const FALLBACK: ChartUi = {
  text: '#e8ebf2',
  muted: '#8b93a7',
  border: '#262e40',
  borderSoft: '#1e2534',
  panel: '#141926',
  panel2: '#1b2130',
  success: '#6ee7b7',
  danger: '#ff6b6b',
  accent: '#4f8cff',
  violet: '#a78bfa',
  isLight: false,
}

/**
 * A theme colour with transparency, for the heatmaps' scales (the tokens are #rrggbb
 * hex; another format passes as it is, without transparency).
 */
export function withAlpha(colour: string, alpha: number): string {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(colour.trim())
  if (!m) return colour
  return `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${alpha})`
}

export function useChartTheme() {
  const { theme } = useTheme()

  function read(): ChartUi {
    if (!import.meta.client) return FALLBACK
    const s = getComputedStyle(document.documentElement)
    const g = (n: string, f: string) => s.getPropertyValue(n).trim() || f
    return {
      text: g('--text', FALLBACK.text),
      muted: g('--muted', FALLBACK.muted),
      border: g('--border', FALLBACK.border),
      borderSoft: g('--border-soft', FALLBACK.borderSoft),
      panel: g('--panel', FALLBACK.panel),
      panel2: g('--panel-2', FALLBACK.panel2),
      success: g('--accent-2', FALLBACK.success),
      danger: g('--danger', FALLBACK.danger),
      accent: g('--accent', FALLBACK.accent),
      violet: g('--violet', FALLBACK.violet),
      // not from the name: the light themes are more than one, and each declares its color-scheme
      isLight: s.getPropertyValue('color-scheme').trim() === 'light',
    }
  }

  const ui = ref<ChartUi>(read())
  // the theme change rewrites the tokens on :root: they must be read again, not recomputed
  watch(theme, () => {
    ui.value = read()
  })

  return { ui }
}
