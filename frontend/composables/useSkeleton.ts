// Tempo minimo di visibilità degli skeleton: un caricamento da 40ms farebbe
// lampeggiare lo shimmer come un glitch. Non è uno sleep fisso: si attende
// solo la DIFFERENZA, quindi i caricamenti lenti non vengono rallentati.
export const SKELETON_MIN_MS = 300

/** Attende quel che manca perché lo skeleton resti visibile almeno `min` ms
 *  dall'istante `t0` (preso con performance.now() all'inizio del load). */
export async function skeletonPad(t0: number, min = SKELETON_MIN_MS): Promise<void> {
  const wait = min - (performance.now() - t0)
  if (wait > 0) await new Promise((r) => setTimeout(r, wait))
}
