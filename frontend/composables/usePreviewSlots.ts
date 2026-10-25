// "Slot" previews: in every slot only the LAST request counts.
//
// Whoever draws a flow clicks one node after another. Every click launched one or more
// previews and none was ever cancelled: the result of the overtaken ones was dropped
// (previewSeq), but the request went to the end — and on the worker they stayed queued
// IN FRONT of the only one that mattered. Measured on a big table: eight clicks, the last
// preview on screen after 79 s instead of 25, and five of the eight failed for memory.
//
// Two halves of the same remedy:
//  - here: the same slot's previous request is CANCELLED (it frees the browser's
//    connections: they are 6 per host, and ten pending previews blocked the page's other
//    calls too);
//  - on the server: the `slot` tells the backend to knock down the preview that held it —
//    revoking the task and KILLing the query. The HTTP cancellation alone does not get
//    that far (ingress, gateway, synchronous route).
import { useApi, type PreviewResult } from './useApi'

type PreviewBody = Parameters<ReturnType<typeof useApi>['preview']>[0]

/** True when the error is only "overtaken by a newer one": never to show. */
export function isSuperseded(e: any): boolean {
  const code = e?.response?.status ?? e?.statusCode
  if (code === 409) return true
  // ofetch always wraps in a FetchError with its own `name`: the AbortError sits in `cause`
  if (e?.name === 'AbortError' || e?.cause?.name === 'AbortError') return true
  return /abort/i.test(String(e?.cause?.message ?? e?.message ?? ''))
}

function makeId(): string {
  // randomUUID exists only in a secure context (https/localhost)
  const c: any = globalThis.crypto
  const raw = c?.randomUUID ? c.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36)
  return raw.replace(/[^A-Za-z0-9]/g, '').slice(0, 12)
}

export function usePreviewSlots(scope: string) {
  const api = useApi()
  // one id per INSTANCE: two tabs of the same user do not cancel each other
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

  /** Cancels the request in flight of ONE slot only (e.g. a deleted node's preview). */
  function cancel(slot: string) {
    inflight.get(slot)?.abort()
    inflight.delete(slot)
  }

  /** At the page's closing: no orphaned work on the server. */
  function cancelAll() {
    for (const ctl of inflight.values()) ctl.abort()
    inflight.clear()
  }

  return { preview, cancel, cancelAll }
}
