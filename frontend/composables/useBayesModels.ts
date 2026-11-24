// Saved Bayesian models: a graph kept in a folder with its versions and the Stan program
// the gateway generates from it (docs/specs/bayes-model-store.md). The rules are the
// gateway's (api/bayes_models.go): a model is seen by whoever has VIEW on its folder AND
// may use the `bayes` group; for anyone else it does not exist (404, one body for absent
// and not seen). Saving a graph with an error is refused (422 with its findings).
import type { BayesRunFacts, BayesRunListed } from '~/composables/bayesAssist'

/** What the graph is, derived by the gateway at every save, never chosen by the person. */
export type BayesFamily = 'hierarchical' | 'series' | 'glm' | 'free'

/** The compile of a program: `none` is «compiled the first time it runs»; `timeout` is a program
 * that passed the compile's deadline or memory, not compiled again until an administrator raises
 * that limit. There is no «with errors» state: a model with errors is never stored. */
export type BayesCompileState = 'none' | 'queued' | 'running' | 'ready' | 'failed' | 'timeout'

export interface BayesCompile {
  state: BayesCompileState
  /** a sentence for the page: why it waits, when it compiled, why it failed or passed a limit */
  detail: string
  at: string | null
}

export interface BayesModel {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  current_version: number
  /** the template it was opened from, if any (the family does not follow it) */
  template_id: string | null
  template_version: number | null
  family: BayesFamily | ''
  created_at: string
  updated_at: string
  /** EDIT on its folder */
  editable: boolean
  /** RUN on its folder */
  runnable: boolean
}

/** A model of GET /bayes-models and GET /projects/{id}/bayes-models: the current version's
 * compile, the flows that pin the model and who saved the current version, all from one read. */
export interface BayesModelListed extends BayesModel {
  compile: BayesCompile
  /** flows that pin the model, counted whether or not this reader may open them */
  uses: number
  /** the email of who saved the current version; null when that person is gone */
  saved_by: string | null
}

/** One version in full (the current one in the detail, any one from `version()`). */
export interface BayesModelVersion {
  version: number
  /** a code the interface translates: created, edited, layout, restored from vN */
  note: string
  created_at: string
  created_by: number | null
  /** the graph, as the editor reads it */
  graph: Record<string, any>
  format_version: number
  semantic_hash: string
  /** the checks' findings of the saved graph: warnings only (an error is never saved) */
  findings: BayesFinding[]
  /** the Stan program, text; read only */
  program: string
  program_sha256: string
  mapping: unknown
  data_manifest: unknown
  quantity_manifest: unknown
  codegen_version: number
  family: BayesFamily
  catalog_sha256: string
  /** the generator's version now: a version with an older one runs as stored */
  codegen_current: number
  current: boolean
}

export interface BayesFinding {
  /** error, warning or info */
  severity?: string
  code?: string
  message?: string
  node?: string
  [key: string]: unknown
}

export interface BayesModelDetail extends BayesModel {
  version: BayesModelVersion
  compile: BayesCompile
  /** flows that pin the model */
  uses: number
}

/** A version in the list of versions: no graph, no program text. */
export interface BayesVersionListed {
  version: number
  note: string
  created_at: string
  /** an email (as the scripts' versions) */
  created_by: string | null
  semantic_hash: string
  program_sha256: string
  codegen_version: number
  family: BayesFamily
  current: boolean
}

/** A flow node that pins the model. `hidden` counts the flows in folders this reader cannot open. */
export interface BayesUses {
  flows: { flow_id: number; flow_name: string; node_id: string; version: number }[]
  hidden: number
}

export interface BayesCheck {
  /** false when the graph has an error: nothing can be saved */
  ok: boolean
  findings: BayesFinding[]
  /** with ok: the program and what it takes */
  program?: string
  program_sha256?: string
  mapping?: unknown
  data_manifest?: unknown
  quantity_manifest?: unknown
  codegen_version?: number
  family?: BayesFamily
  semantic_hash?: string
}

export interface BayesModelCreate {
  name: string
  description?: string
  graph: Record<string, any>
  template_id?: string
  template_version?: number
}

/** A save of the graph names the version it was made from (`base_version`): another save
 * meanwhile is a 409 with `current_version`. A rename, a description or a move needs none. */
export interface BayesModelUpdate {
  name?: string
  description?: string
  project_id?: number
  graph?: Record<string, any>
  base_version?: number
}

/** The 409 of a stale save: the page reloads to see the version someone else saved. */
export interface BayesStale {
  detail: string
  current_version: number
}

/** The 422 of a save or a create whose graph has an error. */
export interface BayesRefused {
  detail: string
  findings: BayesFinding[]
}

/** The 409 of a delete while flows pin the model: their names (`more`: how many are not named). */
export interface BayesInUse {
  detail: string
  flows: string[]
  more?: number
}

export function useBayesModels() {
  const { apiFetch } = useApiClient()
  return {
    list: () => apiFetch<BayesModelListed[]>('/bayes-models'),
    listByProject: (projectId: number) => apiFetch<BayesModelListed[]>(`/projects/${projectId}/bayes-models`),
    get: (id: number) => apiFetch<BayesModelDetail>(`/bayes-models/${id}`),
    create: (projectId: number, body: BayesModelCreate) =>
      apiFetch<BayesModelDetail>(`/projects/${projectId}/bayes-models`, { method: 'POST', body }),
    update: (id: number, body: BayesModelUpdate) =>
      apiFetch<BayesModelDetail>(`/bayes-models/${id}`, { method: 'PATCH', body }),
    duplicate: (id: number) => apiFetch<BayesModelDetail>(`/bayes-models/${id}/duplicate`, { method: 'POST' }),
    remove: (id: number) => apiFetch<void>(`/bayes-models/${id}`, { method: 'DELETE' }),
    versions: (id: number) => apiFetch<BayesVersionListed[]>(`/bayes-models/${id}/versions`),
    /** version 0 is the current one */
    version: (id: number, version: number) => apiFetch<BayesModelVersion>(`/bayes-models/${id}/versions/${version}`),
    restore: (id: number, version: number) =>
      apiFetch<BayesModelDetail>(`/bayes-models/${id}/versions/${version}/restore`, { method: 'POST' }),
    /** compiles that version's program now (RUN); 409 while it is queued or running, and 409 with
     * the state's sentence while it is not compiled again (a limit passed and not raised since) */
    compile: (id: number, version: number) =>
      apiFetch<BayesCompile>(`/bayes-models/${id}/versions/${version}/compile`, { method: 'POST' }),
    uses: (id: number) => apiFetch<BayesUses>(`/bayes-models/${id}/uses`),
    /** the Bayesian nodes of flows the person may open that pin the model, each with its latest sampling run */
    runs: (id: number) => apiFetch<{ runs: BayesRunListed[], hidden: number }>(`/bayes-models/${id}/runs`),
    /** one run's sampler facts and each node's extremes of R-hat and effective sample size (no summary row, no data) */
    runFacts: (id: number, run: number) => apiFetch<BayesRunFacts>(`/bayes-models/${id}/runs/${run}/facts`),
    /** the graph's findings and its program, nothing saved */
    check: (graph: Record<string, any>) =>
      apiFetch<BayesCheck>('/bayes-models/check', { method: 'POST', body: { graph } }),
  }
}

/** The family's label key: the catalogs hold `bayesModels.family_<family>`. */
export function bayesFamilyKey(family: string | null | undefined): string {
  return `bayesModels.family_${family === 'hierarchical' || family === 'series' || family === 'glm' ? family : 'free'}`
}
