// The state of the editor of a Bayesian model (docs/specs/bayes-graph-editor.md): the graph document,
// the selection, the history, and the mirror's reading of the document after every change (the
// findings on the nodes, the prior plan the charts draw from).
//
// The store (lot G3): the document as last saved (or opened) is kept as its JSON, so the page knows
// when there are unsaved changes; the gateway's findings of a save (the 422 of a graph with an error,
// the warnings of a saved version) replace the mirror's for as long as the document is the one that
// was sent, and the mirror speaks again at the first change. A version read only (`readonly`)
// takes no edit, no undo and no redo.
//
// The history keeps snapshots of the document's JSON (createFlowHistory, as the flow editor): a
// gesture (a node added, a drop, a resize, an edge) is one step at once; typing in a field becomes a
// step when it stops for a moment. Undo and redo put a snapshot back; the canvas draws it again.
//
// Nothing of Nuxt here (only Vue's reactivity and the pure modules), so the tests run it in Node.
import { computed, ref, shallowRef, watch } from 'vue'
import { createFlowHistory } from '~/composables/useFlowHistory'
import { bayesGraphDecode, bayesGraphModel, bayesPriorPlan, type BayesGraphFinding, type BayesModel, type BayesPriorPlan } from '~/composables/bayesGraph'
import type { BayesDoc } from '~/composables/bayesGraphEdits'

export interface BayesEditorOptions {
  /** How long a change must stay still to be a step (ms). */
  settleMs?: number
  /** For the tests: a fake clock. */
  schedule?: (fn: () => void, ms: number) => unknown
  cancel?: (handle: unknown) => void
}

export function useBayesEditor (opts: BayesEditorOptions = {}) {
  const doc = ref<BayesDoc | null>(null)
  const selected = ref<string | null>(null)
  const canUndo = ref(false)
  const canRedo = ref(false)
  /** Bumped by undo and redo: the canvas draws the document again from scratch. */
  const restored = ref(0)
  /** A version looked at, not the current one: nothing changes it. */
  const readonly = ref(false)

  // The document as text: what the mirror decodes, what the history keeps, what a save sends.
  const text = computed(() => (doc.value ? JSON.stringify(doc.value) : ''))
  // The document is decoded and validated again after every change: a graph is at most 200 nodes,
  // and the mirror's time on a template is a few milliseconds.
  const decoded = computed(() => (doc.value ? bayesGraphDecode(text.value) : null))
  const model = computed<BayesModel | null>(() => (decoded.value?.graph ? bayesGraphModel(decoded.value.graph) : null))
  const mirrorFindings = computed<BayesGraphFinding[]>(() => [...(decoded.value?.findings ?? []), ...(model.value?.findings ?? [])])
  // the gateway's answer for one document: authoritative while the document is that one
  const answered = shallowRef<{ text: string, findings: BayesGraphFinding[] } | null>(null)
  const fromGateway = computed(() => !!answered.value && answered.value.text === text.value)
  const findings = computed<BayesGraphFinding[]>(() => (fromGateway.value ? answered.value!.findings : mirrorFindings.value))
  // the document as last saved or opened; null while nothing is open
  const savedText = ref<string | null>(null)
  const dirty = computed(() => savedText.value !== null && !!doc.value && text.value !== savedText.value)
  const plan = shallowRef<BayesPriorPlan | null>(null)
  watch(model, (m) => { plan.value = m ? bayesPriorPlan(m) : null }, { immediate: true })

  const exists = (d: BayesDoc, id: string) => d.nodes.some(n => n.id === id) || d.plates.some(p => p.id === id)

  const history = createFlowHistory({
    serialize: () => (doc.value ? JSON.stringify(doc.value) : ''),
    restore: (s) => {
      const d = JSON.parse(s) as BayesDoc
      doc.value = d
      if (selected.value && !exists(d, selected.value)) selected.value = null
      restored.value++
    },
    onChange: () => { canUndo.value = history.canUndo(); canRedo.value = history.canRedo() },
    settleMs: opts.settleMs ?? 400,
    schedule: opts.schedule,
    cancel: opts.cancel,
  })
  // whatever changes the document arms the history (a safety net: every edit below arms it too)
  watch(doc, () => history.touch(), { deep: true })

  /** A document opened: the history starts again from it, and it is what «unsaved changes» compares with. */
  function load (d: BayesDoc | null): void {
    doc.value = d
    selected.value = null
    answered.value = null
    history.reset()
    history.flush()
    savedText.value = d ? text.value : null
    restored.value++
  }

  /**
   * Another document in place of this one as ONE step of the history: a template opened on the canvas,
   * the version someone else saved (a stale save's «Reload»). Undo brings back what was there.
   */
  function adopt (d: BayesDoc): void {
    history.flush()
    doc.value = d
    if (selected.value && !exists(d, selected.value)) selected.value = null
    history.touch()
    history.flush()
    restored.value++
  }

  /** The document `sent` is the saved one now (a save answered 200, or a version loaded in place). */
  function markSaved (sent: string): void { savedText.value = sent }

  /** The gateway's findings for the document `sent`: they stand on the nodes until the document changes. */
  function answer (sent: string, list: BayesGraphFinding[]): void { answered.value = { text: sent, findings: list } }

  /**
   * Runs a change of the document. A gesture (`step`, the default) is one step at once, and what was
   * being typed before it stays a step of its own; typing (`step: false`) becomes one when it stops.
   */
  function edit<T> (change: (d: BayesDoc) => T, step = true): T | undefined {
    const d = doc.value
    if (!d || readonly.value) return undefined
    if (step) history.flush()
    const out = change(d)
    history.touch()
    if (step) history.flush()
    return out
  }

  return {
    doc,
    selected,
    model,
    findings,
    plan,
    canUndo,
    canRedo,
    restored,
    readonly,
    text,
    dirty,
    fromGateway,
    load,
    adopt,
    markSaved,
    answer,
    edit,
    undo: () => (readonly.value ? false : history.undo()),
    redo: () => (readonly.value ? false : history.redo()),
    /** Closes a pending step at once (before leaving, before a save). */
    flush: () => history.flush(),
    dispose: () => history.dispose(),
  }
}

export type BayesEditor = ReturnType<typeof useBayesEditor>
