// The MLflow block of a Bayesian model node (docs/specs/bayes-mlflow.md, lot M3): the same object as a Model node's,
// `{ connectionId, experiment, registerAs?, continueWithout? }`, so the gateway's `planMLflow` and the flow's reach read it
// as it is. The rules are the Model node's (`modelMlflow`, `modelMlflowProblem`); this file only says how the panel changes it.
import { modelMlflow, modelMlflowProblem } from '~/composables/modelCatalog'

type Block = Record<string, any>

/** The connection chosen. None takes the whole block with it, the registered name included (a name without a server to
 *  register on would be a hidden request that a later choice of connection would revive); a connection keeps the rest. */
export function bayesMlflowConnection(block: Block | null | undefined, id: unknown): Block | undefined {
  if (typeof id !== 'number') return undefined
  return modelMlflow({ ...(block ?? {}), connectionId: id })
}

/** One of the block's fields changed (a name is trimmed, an empty registered name goes, `continueWithout` is kept only when on). */
export function bayesMlflowSet(
  block: Block | null | undefined, key: 'experiment' | 'registerAs' | 'continueWithout', value: unknown,
): Block | undefined {
  return modelMlflow({ ...(block ?? {}), [key]: value })
}

/** What the block still lacks: 'experiment' when it has a connection and no experiment name. */
export const bayesMlflowProblem = (block: Block | null | undefined): '' | 'experiment' => modelMlflowProblem(block)
