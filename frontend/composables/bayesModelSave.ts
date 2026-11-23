// Saving a Bayesian model from its editor (docs/specs/bayes-graph-editor.md, «Saving»; the store's
// routes: docs/specs/bayes-model-store.md). A new model is created in its folder; a saved one gets a
// new version, the save naming the version it was made from (`base_version`).
//
// What the gateway answers, in the editor's terms:
// - 200 / 201: saved; the document sent is the saved one, and the version's findings (warnings only)
//   stand on the nodes until the next change;
// - 422 with findings: a graph with an error is never saved; the gateway's findings replace the
//   mirror's (the gateway is the authority), nothing is written;
// - 409 with `current_version`: someone saved meanwhile; the page offers to reload, the person's graph
//   one Undo away;
// - 409 without it: the name is taken in that folder;
// - anything else: the save failed, with the gateway's words.
//
// Nothing of Nuxt here: the client is handed in, so the tests run it with a fake one.
import type { BayesGraphFinding } from '~/composables/bayesGraph'
import type { BayesFinding, BayesModelCreate, BayesModelDetail, BayesModelUpdate } from '~/composables/useBayesModels'

/** The part of the store's client a save uses. */
export interface BayesSaveClient {
  create: (projectId: number, body: BayesModelCreate) => Promise<BayesModelDetail>
  update: (id: number, body: BayesModelUpdate) => Promise<BayesModelDetail>
}

/** The part of the editor a save reads and writes (useBayesEditor). */
export interface BayesSaveEditor {
  flush: () => void
  text: { value: string }
  markSaved: (sent: string) => void
  answer: (sent: string, findings: BayesGraphFinding[]) => void
}

/** Which model, and what the page holds besides the graph. */
export interface BayesSaveTarget {
  /** null: a model never saved, created in `projectId` */
  id: number | null
  projectId: number
  /** the version the page opened (or last saved); required for a saved model */
  baseVersion: number | null
  name: string
  /** the name and the folder as saved, so that only what changed is sent */
  savedName: string | null
  savedProjectId: number | null
  templateId?: string | null
  templateVersion?: number | null
}

export type BayesSaveOutcome =
  | { kind: 'saved', detail: BayesModelDetail, created: boolean }
  | { kind: 'refused', errors: number, warnings: number }
  | { kind: 'stale', current: number }
  | { kind: 'name' }
  | { kind: 'failed', status: number, detail: string }

/** The gateway's findings in the mirror's shape (Go leaves out an empty node or slot). */
export function bayesGatewayFindings (list: readonly BayesFinding[] | null | undefined): BayesGraphFinding[] {
  return (list ?? []).filter(f => typeof f?.code === 'string').map(f => ({
    code: String(f.code),
    severity: (f.severity === 'error' || f.severity === 'warning' ? f.severity : 'warning') as BayesGraphFinding['severity'],
    node: typeof f.node === 'string' ? f.node : '',
    slot: typeof f.slot === 'string' ? f.slot : '',
    ...(f.args && typeof f.args === 'object' ? { args: f.args as Record<string, unknown> } : {}),
  }))
}

/** The status and body of a failed request ($fetch puts them in `response.status` and `data`). */
export function bayesFetchError (e: unknown): { status: number, data: Record<string, unknown> } {
  const x = e as { response?: { status?: number }, status?: number, statusCode?: number, data?: unknown } | null
  const status = Number(x?.response?.status ?? x?.status ?? x?.statusCode ?? 0) || 0
  const data = x?.data && typeof x.data === 'object' ? x.data as Record<string, unknown> : {}
  return { status, data }
}

export async function bayesSaveModel (editor: BayesSaveEditor, client: BayesSaveClient, target: BayesSaveTarget): Promise<BayesSaveOutcome> {
  // what was being typed is part of what is saved
  editor.flush()
  const sent = editor.text.value
  const graph = JSON.parse(sent) as Record<string, any>
  const name = target.name.trim()
  try {
    let detail: BayesModelDetail
    const created = target.id === null
    if (target.id === null) {
      detail = await client.create(target.projectId, {
        name,
        graph,
        ...(target.templateId ? { template_id: target.templateId } : {}),
        ...(target.templateId && target.templateVersion ? { template_version: target.templateVersion } : {}),
      })
    } else {
      const body: BayesModelUpdate = { graph, base_version: target.baseVersion ?? 0 }
      if (name !== target.savedName) body.name = name
      if (target.projectId !== target.savedProjectId) body.project_id = target.projectId
      detail = await client.update(target.id, body)
    }
    editor.markSaved(sent)
    editor.answer(sent, bayesGatewayFindings(detail.version?.findings))
    return { kind: 'saved', detail, created }
  } catch (e) {
    const { status, data } = bayesFetchError(e)
    if (status === 422 && Array.isArray(data.findings)) {
      const list = bayesGatewayFindings(data.findings as BayesFinding[])
      const errors = list.filter(f => f.severity === 'error').length
      if (errors > 0) {
        editor.answer(sent, list)
        return { kind: 'refused', errors, warnings: list.length - errors }
      }
    }
    if (status === 409 && typeof data.current_version === 'number') return { kind: 'stale', current: data.current_version }
    if (status === 409) return { kind: 'name' }
    return { kind: 'failed', status, detail: typeof data.detail === 'string' ? data.detail : String((e as Error)?.message ?? '') }
  }
}

/** A version's note in the person's words: the key under bayes.editor.versions.note and its values. */
export function bayesVersionNote (note: string): { key: string, params: Record<string, string | number> } {
  const restored = /^restored from v(\d+)$/.exec(note)
  if (restored) return { key: 'restored', params: { n: Number(restored[1]) } }
  const dup = /^duplicated from «(.*)» v(\d+)$/.exec(note)
  if (dup) return { key: 'duplicated', params: { name: dup[1]!, n: Number(dup[2]) } }
  if (note === 'created' || note === 'edited' || note === 'layout') return { key: note, params: {} }
  return { key: 'other', params: { note } }
}

/**
 * A version older than the current one: whether the newer versions change the model or only its
 * drawing (the same semantic hash), for the notice «a newer version exists».
 */
export function bayesNewerVersion (
  versions: ReadonlyArray<{ version: number, semantic_hash: string, current: boolean }>, shown: number,
): { current: number, layoutOnly: boolean } | null {
  const cur = versions.find(v => v.current) ?? [...versions].sort((a, b) => b.version - a.version)[0]
  const here = versions.find(v => v.version === shown)
  if (!cur || !here || cur.version <= shown) return null
  return { current: cur.version, layoutOnly: cur.semantic_hash === here.semantic_hash }
}
