// Colori "cromo" dei grafici (assi, etichette, bordi, sfondo delle tooltip) letti
// dai token CSS del tema corrente.
//
// ECharts non legge le variabili CSS: ogni opzione vuole un colore già risolto.
// La stessa funzione `readUi()` era copiata in tre componenti (audit, ScheduleLoad,
// ChartPanel) e mancava negli altri due (RunCalendar, RunGantt), che per questo
// restavano tarati sul tema scuro qualunque tema fosse attivo. Qui sta una volta
// sola, con il sovrainsieme dei token che i cinque usano.
//
// I colori DATO (rampe, serie) restano nei componenti: sono codifica, non cromo.
import { ref, watch } from 'vue'
import { useTheme } from '~/composables/useTheme'

export interface ChartUi {
  text: string
  muted: string
  border: string
  borderSoft: string
  panel: string
  panel2: string
  /** --accent-2: esito positivo */
  success: string
  /** --danger: esito negativo */
  danger: string
  /** --accent: in corso / selezione */
  accent: string
  /** --violet: terza categoria (es. esecuzioni, refresh) */
  violet: string
  /** il tema ha lo sfondo chiaro (light, imperium): lo dice il suo `color-scheme` */
  isLight: boolean
}

// usato dal render lato server, dove non esiste `document`: sono i valori del
// tema scuro, che è il default senza `data-theme`
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

/** Un colore del tema con trasparenza, per le scale delle heatmap (i token sono
 *  esadecimali #rrggbb; un formato diverso passa com'è, senza trasparenza). */
export function withAlpha(colore: string, alpha: number): string {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(colore.trim())
  if (!m) return colore
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
      // non dal nome: i temi chiari sono più d'uno, e ognuno dichiara il suo color-scheme
      isLight: s.getPropertyValue('color-scheme').trim() === 'light',
    }
  }

  const ui = ref<ChartUi>(read())
  // il cambio tema riscrive i token su :root: vanno riletti, non ricalcolati
  watch(theme, () => {
    ui.value = read()
  })

  return { ui }
}
