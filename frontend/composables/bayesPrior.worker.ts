// The priors' preview worker (docs/specs/bayes-prior-preview.md, «The worker»): it receives a prior
// plan, the quantity to show and a seed, and answers with the chart's arrays, transferred, not
// copied. It is loaded by composables/bayesPriorPreview.ts the first time a prior's chart is asked
// for, so stdlib and the core stay out of the entry chunk.
//
// It has no export on purpose: a composable's exports are auto-imported, and a worker's file is
// not something a page may import.
//
// One request at a time: the core is synchronous, so messages are answered in the order they came.
// The page sends a new request only after the answer of the previous one, and drops what is stale.
import { bayesPriorPreview } from '~/composables/bayesPriorCore'
import type { BayesPriorAnswer } from '~/composables/bayesPriorCore'
import type { BayesPriorPlan } from '~/composables/bayesGraph'

interface Request { id: number, plan: BayesPriorPlan, target: string, seed: number }

// the worker's global scope, typed by what is used here (the app's tsconfig sees the DOM's `self`)
const scope = self as unknown as {
  onmessage: ((event: { data: Request }) => void) | null
  postMessage: (message: unknown, transfer?: Transferable[]) => void
}

scope.onmessage = (event) => {
  const { id, plan, target, seed } = event.data
  try {
    const answer: BayesPriorAnswer = bayesPriorPreview(plan, target, seed)
    const transfer: Transferable[] = []
    if (answer.x) transfer.push(answer.x.buffer)
    if (answer.y) transfer.push(answer.y.buffer)
    scope.postMessage({ id, answer }, transfer)
  } catch (error) {
    // the core answers `errors` for what it can foresee; this is what it could not: said, never thrown
    console.error('prior preview:', error)
    scope.postMessage({ id, failed: true })
  }
}
