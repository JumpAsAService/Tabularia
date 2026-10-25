// The user's favourite engine. Like the theme: a value persisted in localStorage, shared
// at module level. It is the DEFAULT where it is not asked explicitly (Viewer, a new flow
// opened without a choice); at the flow's CREATION the engine stays chosen by hand from
// the dedicated menu.
import { ref } from 'vue'
import { useApi } from '~/composables/useApi'

// `optional`: an engine that exists only when configured on the server (not available = not configured, not "coming")
// `disabled_by_admin`: available on the server but FORBIDDEN in this installation (Admin
// panel → Engines). It must be told apart from `optional`: «not allowed here» is not «not
// configured», and mixing them up sends the user looking for an environment variable
// that has nothing to do with it.
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
// catalog of the available engines, loaded once and shared
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

  // loads the catalog once only (idempotent): useful for the selector in Settings. On error
  // it leaves the Polars fallback and will retry.
  async function loadCatalog() {
    if (catalogLoaded) return
    catalogLoaded = true
    try { catalog.value = await api.engines() } catch { catalogLoaded = false }
  }

  // it is saved here, when it is chosen: a `watch` created by the first component died with
  // it, and after a page change the choice was no longer saved
  function setPreferredEngine(id: string) {
    preferredEngine.value = id
    try { localStorage.setItem(STORAGE_KEY, id) } catch { /* storage denied */ }
  }

  // engine to use as default: the favourite if (still) available, otherwise the catalog's
  // first available engine (robust fallback).
  function defaultEngine(available?: { id: string; available: boolean }[]): string {
    const list = available ?? catalog.value
    const ok = list.find((e) => e.id === preferredEngine.value && e.available)
    if (ok) return preferredEngine.value
    return list.find((e) => e.available)?.id ?? 'polars'
  }

  return { preferredEngine, setPreferredEngine, engineCatalog: catalog, loadCatalog, defaultEngine }
}
