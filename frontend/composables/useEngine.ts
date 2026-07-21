// Motore preferito dell'utente. Come il tema: un valore persistito in
// localStorage, condiviso a livello di modulo. È il DEFAULT dove non viene
// chiesto esplicitamente (Viewer, nuovo flusso aperto senza scelta); alla
// CREAZIONE del flusso il motore resta scelto a mano dal menù dedicato.
import { ref, watch } from 'vue'
import { useApi } from '~/composables/useApi'

export interface EngineOpt { id: string; label: string; available: boolean; description: string }

const STORAGE_KEY = 'tabularia-engine'

const preferredEngine = ref<string>('polars')
// catalogo degli engine disponibili, caricato una volta e condiviso
const catalog = ref<EngineOpt[]>([{ id: 'polars', label: 'Polars', available: true, description: '' }])
let prefInit = false
let catalogLoaded = false

export function usePreferredEngine() {
  const api = useApi()

  if (!prefInit && import.meta.client) {
    prefInit = true
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) preferredEngine.value = saved
    watch(preferredEngine, (e) => {
      try { localStorage.setItem(STORAGE_KEY, e) } catch { /* storage negato */ }
    })
  }

  // carica il catalogo una sola volta (idempotente): utile per il selettore in
  // Impostazioni. Su errore lascia il fallback Polars e riproverà.
  async function loadCatalog() {
    if (catalogLoaded) return
    catalogLoaded = true
    try { catalog.value = await api.engines() } catch { catalogLoaded = false }
  }

  function setPreferredEngine(id: string) {
    preferredEngine.value = id
  }

  // motore da usare come default: la preferita se (ancora) disponibile, altrimenti
  // il primo engine disponibile del catalogo (fallback robusto).
  function defaultEngine(available?: { id: string; available: boolean }[]): string {
    const list = available ?? catalog.value
    const ok = list.find((e) => e.id === preferredEngine.value && e.available)
    if (ok) return preferredEngine.value
    return list.find((e) => e.available)?.id ?? 'polars'
  }

  return { preferredEngine, setPreferredEngine, engineCatalog: catalog, loadCatalog, defaultEngine }
}
