// The page's side of the priors' preview (docs/specs/bayes-prior-preview.md): it asks the worker for
// the chart of one quantity's prior and keeps the answer in a reactive state for the panel.
//
// The worker is started the first time a chart is asked for, never with the page: stdlib and the
// core are a chunk of their own. The page waits 150 ms after the last change before it asks (the
// spec's decision), asks again only when the worker has answered the previous request, and shows
// only the answer of its latest request: a slow redraw never covers a newer one.
//
// This file imports the core's types only, never the core: it is in the entry chunk's reach.
// Every export starts with `bayes` or `useBayes`: a composable's exports are auto-imported everywhere.
import { onScopeDispose, shallowRef } from 'vue'
import type { BayesPriorAnswer } from '~/composables/bayesPriorCore'
import type { BayesPriorPlan } from '~/composables/bayesGraph'

/** The quiet time after the last change before a chart is asked for (the spec's decision). */
export const BAYES_PRIOR_DEBOUNCE_MS = 150

/** What the panel reads: the answer to show, whether a newer one is on its way, whether the last one failed. */
export interface BayesPriorPreviewState {
  answer: BayesPriorAnswer | null
  pending: boolean
  failed: boolean
}

/** The part of a Worker the client uses (the tests give it a fake one). */
export interface BayesPriorWorkerLike {
  onmessage: ((event: { data: any }) => void) | null
  onerror: ((event: unknown) => void) | null
  postMessage(message: unknown): void
  terminate(): void
}

export interface BayesPriorPreviewClient {
  /** Asks for a chart: the previous request that has not started is dropped. */
  request(plan: BayesPriorPlan, target: string, seed: number): void
  /** Drops what is waiting and what is on its way; the state keeps its last answer. */
  cancel(): void
  /** Cancels and stops the worker. */
  dispose(): void
}

interface Waiting { plan: BayesPriorPlan, target: string, seed: number }

/**
 * The client without Vue or the browser: `start` makes the worker (the lazy import of the real one),
 * `update` is called with every new state. The page's composable and the tests use this.
 */
export function bayesPriorPreviewClient (
  start: () => Promise<BayesPriorWorkerLike>,
  update: (state: BayesPriorPreviewState) => void,
  delay: number = BAYES_PRIOR_DEBOUNCE_MS,
): BayesPriorPreviewClient {
  let timer: ReturnType<typeof setTimeout> | null = null
  let waiting: Waiting | null = null // the latest request, not sent yet
  let inFlight = 0 // the id the worker is working on, 0 when it is free
  let latest = 0 // the id of the latest request sent
  let stale = false // what is in flight was cancelled: its answer is dropped
  let worker: BayesPriorWorkerLike | null = null
  let starting: Promise<BayesPriorWorkerLike> | null = null
  let disposed = false
  let current: BayesPriorPreviewState = { answer: null, pending: false, failed: false }

  const set = (next: Partial<BayesPriorPreviewState>) => {
    current = { ...current, ...next }
    update(current)
  }

  const cloneable = (plan: BayesPriorPlan) => {
    // a reactive proxy cannot be cloned by postMessage: a plain copy of the plan can
    try { structuredClone(plan); return plan } catch { return JSON.parse(JSON.stringify(plan)) as BayesPriorPlan }
  }

  const send = async () => {
    if (!waiting || inFlight || disposed) return
    const { plan, target, seed } = waiting
    waiting = null
    const id = ++latest
    inFlight = id
    try {
      if (!worker) {
        starting ??= start()
        worker = await starting
        worker.onmessage = (event) => answered(event.data)
        worker.onerror = () => answered({ id: inFlight, failed: true })
      }
      if (disposed) return
      worker.postMessage({ id, plan: cloneable(plan), target, seed })
    } catch (error) {
      console.error('prior preview:', error)
      starting = null
      worker = null
      inFlight = 0
      set({ pending: !!waiting, failed: true })
    }
  }

  const answered = (data: { id: number, answer?: BayesPriorAnswer, failed?: boolean }) => {
    if (disposed || data.id !== inFlight) return
    inFlight = 0
    const dropped = stale
    stale = false
    if (waiting) {
      // a newer request came while this one ran: this answer is stale, the newer one is sent now
      void send()
      return
    }
    if (dropped) return
    if (data.failed || !data.answer) set({ pending: false, failed: true })
    else set({ answer: data.answer, pending: false, failed: false })
  }

  return {
    request (plan, target, seed) {
      if (disposed) return
      waiting = { plan, target, seed }
      // a failure of an earlier request is not this one's: it must not be read as the answer to it
      set({ pending: true, failed: false })
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { timer = null; void send() }, delay)
    },
    cancel () {
      if (timer) { clearTimeout(timer); timer = null }
      waiting = null
      // what is on its way is dropped when it comes back
      stale = inFlight !== 0
      set({ pending: false })
    },
    dispose () {
      this.cancel()
      disposed = true
      worker?.terminate()
      worker = null
    },
  }
}

/** The real worker, made the first time it is needed: a chunk of its own, with stdlib in it. */
async function startWorker (): Promise<BayesPriorWorkerLike> {
  const { default: PriorWorker } = await import('~/composables/bayesPrior.worker?worker')
  return new PriorWorker() as unknown as BayesPriorWorkerLike
}

/**
 * For a panel: `state.value` is what to draw, `request` asks for a chart (debounced), and the
 * worker stops with the component's scope. Each call makes its own client; the worker is started
 * on its first request.
 */
export function useBayesPriorPreview (delay: number = BAYES_PRIOR_DEBOUNCE_MS) {
  const state = shallowRef<BayesPriorPreviewState>({ answer: null, pending: false, failed: false })
  const client = bayesPriorPreviewClient(startWorker, (next) => { state.value = next }, delay)
  onScopeDispose(() => client.dispose())
  return { state, request: client.request, cancel: client.cancel }
}
