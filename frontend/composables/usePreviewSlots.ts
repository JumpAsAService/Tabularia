// Preview "a slot": in ogni slot conta solo l'ULTIMA richiesta.
//
// Chi disegna un flusso clicca un nodo dopo l'altro. Ogni click lanciava una o
// piu' preview e nessuna veniva mai annullata: il risultato di quelle superate
// era scartato (previewSeq), ma la richiesta andava fino in fondo — e sul worker
// restavano in coda DAVANTI all'unica che interessava. Misurato su una tabella
// grande: otto click, l'ultima preview a schermo dopo 79 s invece di 25, e
// cinque delle otto fallite per memoria.
//
// Due meta' dello stesso rimedio:
//  - qui: la richiesta precedente dello stesso slot viene ANNULLATA (libera le
//    connessioni del browser: sono 6 per host, e dieci preview pendenti
//    bloccavano anche le altre chiamate della pagina);
//  - sul server: lo `slot` dice al backend di buttare giu' la preview che lo
//    occupava — revoca del task e KILL della query. L'annullamento HTTP da solo
//    non arriva fin li' (ingress, gateway, route sincrona).
import { useApi, type PreviewResult } from './useApi'

type PreviewBody = Parameters<ReturnType<typeof useApi>['preview']>[0]

/** True se l'errore e' solo "superata da una piu' recente": mai da mostrare. */
export function isSuperseded(e: any): boolean {
  const code = e?.response?.status ?? e?.statusCode
  if (code === 409) return true
  // ofetch avvolge sempre in un FetchError con un suo `name`: l'AbortError sta in `cause`
  if (e?.name === 'AbortError' || e?.cause?.name === 'AbortError') return true
  return /abort/i.test(String(e?.cause?.message ?? e?.message ?? ''))
}

function makeId(): string {
  // randomUUID esiste solo in contesto sicuro (https/localhost)
  const c: any = globalThis.crypto
  const raw = c?.randomUUID ? c.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36)
  return raw.replace(/[^A-Za-z0-9]/g, '').slice(0, 12)
}

export function usePreviewSlots(scope: string) {
  const api = useApi()
  // un id per ISTANZA: due schede dello stesso utente non si annullano a vicenda
  const owner = `${scope}-${makeId()}`
  const inflight = new Map<string, AbortController>()

  function preview(body: PreviewBody, slot?: string): Promise<PreviewResult> {
    if (!slot) return api.preview(body)
    inflight.get(slot)?.abort()
    const ctl = new AbortController()
    inflight.set(slot, ctl)
    return api.preview({ ...body, slot: `${owner}-${slot}` }, { signal: ctl.signal }).finally(() => {
      if (inflight.get(slot) === ctl) inflight.delete(slot)
    })
  }

  /** Annulla la richiesta in volo di UN solo slot (es. la preview di un nodo cancellato). */
  function cancel(slot: string) {
    inflight.get(slot)?.abort()
    inflight.delete(slot)
  }

  /** Alla chiusura della pagina: niente lavoro orfano sul server. */
  function cancelAll() {
    for (const ctl of inflight.values()) ctl.abort()
    inflight.clear()
  }

  return { preview, cancel, cancelAll }
}
