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
  /** solo il tema "light" ha sfondo chiaro; dark/dracula/monokai sono scuri */
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
      isLight: document.documentElement.getAttribute('data-theme') === 'light',
    }
  }

  const ui = ref<ChartUi>(read())
  // il cambio tema riscrive i token su :root: vanno riletti, non ricalcolati
  watch(theme, () => {
    ui.value = read()
  })

  return { ui }
}
