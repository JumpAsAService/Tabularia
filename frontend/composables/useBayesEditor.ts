// The state of the editor of a Bayesian model (docs/specs/bayes-graph-editor.md): the graph document,
// the selection, the history, and the mirror's reading of the document after every change (the
// findings on the nodes, the prior plan the charts draw from). The save comes with lot G3.
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

  // The document is decoded and validated again after every change: a graph is at most 200 nodes,
  // and the mirror's time on a template is a few milliseconds.
  const decoded = computed(() => (doc.value ? bayesGraphDecode(JSON.stringify(doc.value)) : null))
  const model = computed<BayesModel | null>(() => (decoded.value?.graph ? bayesGraphModel(decoded.value.graph) : null))
  const findings = computed<BayesGraphFinding[]>(() => [...(decoded.value?.findings ?? []), ...(model.value?.findings ?? [])])
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

  /** A document opened: the history starts again from it. */
  function load (d: BayesDoc | null): void {
    doc.value = d
    selected.value = null
    history.reset()
    history.flush()
    restored.value++
  }

  /**
   * Runs a change of the document. A gesture (`step`, the default) is one step at once, and what was
   * being typed before it stays a step of its own; typing (`step: false`) becomes one when it stops.
   */
  function edit<T> (change: (d: BayesDoc) => T, step = true): T | undefined {
    const d = doc.value
    if (!d) return undefined
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
    load,
    edit,
    undo: () => history.undo(),
    redo: () => history.redo(),
    /** Closes a pending step at once (before leaving, before a save). */
    flush: () => history.flush(),
    dispose: () => history.dispose(),
  }
}

export type BayesEditor = ReturnType<typeof useBayesEditor>
