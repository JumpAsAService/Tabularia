// Motore preferito dell'utente. Come il tema: un valore persistito in
// localStorage, condiviso a livello di modulo. È il DEFAULT dove non viene
// chiesto esplicitamente (Viewer, nuovo flusso aperto senza scelta); alla
// CREAZIONE del flusso il motore resta scelto a mano dal menù dedicato.
import { ref } from 'vue'
import { useApi } from '~/composables/useApi'

// `optional`: engine che esiste solo se configurato lato server (non disponibile = non configurato, non "in arrivo")
// `disabled_by_admin`: disponibile lato server ma VIETATO in questa installazione
// (pannello Admin → Motori). Va distinto da `optional`: «non consentito qui» non
// è «non configurato», e confonderli manda l'utente a cercare una variabile
// d'ambiente che non c'entra.
export interface EngineOpt {
  id: string
  label: string
  available: boolean
  description: string
  optional?: boolean
  disabled_by_admin?: boolean
}

const STORAGE_KEY = 'tabularia-engine'

// Engine descriptions in the language of the interface (i18n `engineDesc.<id>`,
// and `engineDesc.<id>Off` when the engine is not available here). The API's
// text (English) is the fallback for an engine the interface does not know.
// `t`/`te` are the template's `$t`/`$te`: called from templates only.
export function engineDescription(
  e: Pick<EngineOpt, 'id' | 'description' | 'available'>,
  t: (key: string) => string,
  te: (key: string) => boolean,
): string {
  const off = `engineDesc.${e.id}Off`
  if (!e.available && te(off)) return t(off)
  const on = `engineDesc.${e.id}`
  return te(on) ? t(on) : e.description
}

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
  }

  // carica il catalogo una sola volta (idempotente): utile per il selettore in
  // Impostazioni. Su errore lascia il fallback Polars e riproverà.
  async function loadCatalog() {
    if (catalogLoaded) return
    catalogLoaded = true
    try { catalog.value = await api.engines() } catch { catalogLoaded = false }
  }

  // si salva qui, quando la si sceglie: un `watch` creato dal primo componente
  // moriva con lui, e dopo un cambio di pagina la scelta non si salvava più
  function setPreferredEngine(id: string) {
    preferredEngine.value = id
    try { localStorage.setItem(STORAGE_KEY, id) } catch { /* storage negato */ }
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
