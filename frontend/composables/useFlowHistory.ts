// Cronologia dell'editor: annulla / ripeti.
//
// A ISTANTANEE e non a comandi: uno stato è la stessa stringa che si salva
// (`serializeCanvas`), e un passo nasce da solo ogni volta che quella stringa
// cambia e poi resta ferma per un attimo. Così non c'è un elenco di azioni
// «annullabili» da tenere aggiornato: trascinare, collegare, cambiare un
// parametro, riordinare, e qualunque cosa l'editor imparerà a fare domani,
// passano tutte da qui senza che nessuno se ne ricordi.
//
// La pausa (`settleMs`) è ciò che fa di un trascinamento UN passo e non cento, e
// di una parola scritta in un campo un passo e non uno per lettera.
//
// Tre cose NON devono diventare passi, e per tutte vale lo stesso meccanismo —
// «il prossimo stato fermo lo si adotta com'è» (`settling`):
//   - il caricamento del flusso, e le misure che il canvas prende dopo aver
//     disegnato i nodi (cambiano la stringa senza che nessuno abbia fatto niente);
//   - il ripristino fatto da annulla/ripeti: se contasse come modifica
//     cancellerebbe la strada del «ripeti»;
//   - gli aggiornamenti che non vengono dall'utente (una sorgente riagganciata
//     al suo snapshot nuovo): annullarli riporterebbe a una chiave che non c'è più.

export interface FlowHistoryOptions {
  /** Lo stato del canvas, nella forma in cui si salva. */
  serialize: () => string
  /** Rimette il canvas in uno stato preso da `serialize`. */
  restore: (snapshot: string) => void
  /** Chiamata quando cambia ciò che si può annullare o ripetere. */
  onChange?: () => void
  /** Quanti passi si tengono. */
  limit?: number
  /** Quanto deve restare fermo lo stato perché il passo si chiuda (ms). */
  settleMs?: number
  // per i test: un orologio finto
  schedule?: (fn: () => void, ms: number) => unknown
  cancel?: (handle: unknown) => void
}

export function createFlowHistory(opts: FlowHistoryOptions) {
  const limit = opts.limit ?? 100
  const settleMs = opts.settleMs ?? 350
  const schedule = opts.schedule ?? ((fn, ms) => setTimeout(fn, ms))
  const cancel = opts.cancel ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>))

  const past: string[] = []
  const future: string[] = []
  let present: string | null = null
  let settling = true
  let timer: unknown = null

  function arm() {
    if (timer !== null) cancel(timer)
    timer = schedule(settle, settleMs)
  }

  // Lo stato è fermo: o lo si adotta com'è, o diventa un passo.
  function settle() {
    timer = null
    const now = opts.serialize()
    if (settling || present === null) {
      present = now
      settling = false
    } else if (now !== present) {
      past.push(present)
      if (past.length > limit) past.shift()
      present = now
      future.length = 0
    }
    opts.onChange?.()
  }

  /** Chiude subito il passo in sospeso, se c'è. */
  function flush() {
    if (timer === null) return
    cancel(timer)
    settle()
  }

  function step(from: string[], to: string[]): boolean {
    flush() // una modifica ancora «calda» è un passo a sé: va chiusa prima di muoversi
    const target = from.pop()
    if (target === undefined || present === null) return false
    to.push(present)
    present = target
    settling = true
    opts.restore(target)
    arm() // ciò che il canvas aggiusta dopo il ripristino si adotta, non si registra
    opts.onChange?.()
    return true
  }

  return {
    /** Lo stato può essere cambiato (lo chiama chi osserva il canvas). */
    touch: arm,
    undo: () => step(past, future),
    redo: () => step(future, past),
    /** Flusso nuovo o appena caricato: si riparte da qui, senza passi. */
    reset() {
      past.length = 0
      future.length = 0
      present = null
      settling = true
      arm()
      opts.onChange?.()
    },
    /** Esegue una modifica che NON viene dall'utente: non diventa un passo. */
    quietly(change: () => void) {
      flush() // ciò che l'utente stava facendo resta un passo suo
      change()
      settling = true
      arm()
    },
    flush,
    /** L'editor si chiude: nessun passo deve più chiudersi. */
    dispose() {
      if (timer !== null) cancel(timer)
      timer = null
    },
    canUndo: () => past.length > 0,
    canRedo: () => future.length > 0,
  }
}

export type FlowHistory = ReturnType<typeof createFlowHistory>
