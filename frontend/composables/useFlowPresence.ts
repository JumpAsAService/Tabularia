// "Questo flusso è aperto anche da …".
//
// Due persone possono aprire lo stesso flusso, e l'ULTIMO che salva sovrascrive
// l'altro in silenzio. Per scelta non si blocca nulla: si avvisa. L'editor dice
// "sono qui" al gateway ogni pochi secondi e riceve chi altro c'è; chi chiude la
// scheda senza salutare scade da solo lato server.
import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { useApiClient } from './useApiClient'

export interface PresenceOther {
  user_id: number
  email: string
  full_name: string
  since: string
}

export function useFlowPresence(flowId: Ref<number | null>) {
  const { apiFetch } = useApiClient()
  const others = ref<PresenceOther[]>([])
  // un id per SCHEDA: anche una seconda scheda dello stesso utente sovrascrive
  const instance = 'ed-' + (globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2) + Date.now().toString(36)).replace(/[^A-Za-z0-9]/g, '').slice(0, 16)
  let timer: ReturnType<typeof setTimeout> | null = null
  let current: number | null = null
  let every = 15
  let stopped = false // dopo lo smontaggio nessun battito deve piu' partire

  async function beat() {
    if (stopped) return
    const id = flowId.value
    if (id == null || document.hidden) return schedule() // scheda nascosta: niente traffico, si lascia scadere
    try {
      const res = await apiFetch<{ others: PresenceOther[]; heartbeat_seconds: number }>(`/flows/${id}/presence`, {
        method: 'POST',
        body: { instance },
      })
      if (flowId.value === id) others.value = res.others
      every = res.heartbeat_seconds || every
    } catch {
      // l'avviso è una cortesia: se il gateway non risponde (o è una versione che
      // non conosce la rotta) l'editor deve continuare a funzionare in silenzio
      others.value = []
    }
    schedule()
  }
  function schedule() {
    if (timer) clearTimeout(timer)
    // un beat() in volo allo smontaggio risolve DOPO: senza questo rimetterebbe
    // il timer e la presenza continuerebbe per tutta la sessione
    if (stopped) return
    timer = setTimeout(beat, every * 1000)
  }
  function leave(id: number | null) {
    if (id == null) return
    // keepalive: la richiesta parte anche mentre la pagina si sta chiudendo
    apiFetch(`/flows/${id}/presence/${instance}`, { method: 'DELETE', keepalive: true }).catch(() => {})
  }
  const onVisible = () => { if (!document.hidden) beat() }
  const onPageHide = () => leave(current)

  watch(flowId, (id) => {
    if (current !== null && current !== id) leave(current)
    current = id
    others.value = []
    if (id != null) beat()
  }, { immediate: true })

  if (import.meta.client) {
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('pagehide', onPageHide)
  }
  onBeforeUnmount(() => {
    stopped = true
    if (timer) clearTimeout(timer)
    if (import.meta.client) {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('pagehide', onPageHide)
    }
    leave(current)
  })

  return { others }
}
