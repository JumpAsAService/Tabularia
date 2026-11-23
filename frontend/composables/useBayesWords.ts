// The words the inspector and the Problems tab write about a graph, in the person's language
// (docs/specs/bayes-graph-editor.md, «The inspector»): a node's, a box's or a levels plate's label for
// an id, a finding's message with those labels in place of the ids (the mirror names plates and nodes
// by id), the type of a value in words, a slot's name.
import { useI18n } from 'vue-i18n'
import { bayesFindingText } from '~/composables/bayesEditorText'
import { bayesPlateWords } from '~/composables/bayesInspector'
import type { BayesGraphFinding } from '~/composables/bayesGraph'
import type { BayesDoc, BayesSlotPlace } from '~/composables/bayesGraphEdits'

export function useBayesWords (doc: () => BayesDoc | null | undefined) {
  const { t, te } = useI18n()

  /** The label of a node, a component (`<node>.<component>`), a box or a category's levels; undefined when unknown. */
  function labelOrNull (id: string): string | undefined {
    const d = doc()
    if (!d || !id) return undefined
    if (id.startsWith('levels:')) {
      const c = d.nodes.find(n => n.id === id.slice('levels:'.length))
      return c ? t('bayes.editor.type.levelsOf', { name: c.label }) : undefined
    }
    const dot = id.indexOf('.')
    if (dot > 0) {
      const n = d.nodes.find(x => x.id === id.slice(0, dot))
      const part = id.slice(dot + 1)
      if (part === 'correlation') return n?.correlation?.label ?? n?.label
      return n?.components?.find(c => c.id === part)?.label ?? n?.label
    }
    return d.nodes.find(n => n.id === id)?.label ?? d.plates.find(p => p.id === id)?.label
  }
  const labelOf = (id: string): string => labelOrNull(id) ?? id

  /** A finding's message, ids written as labels. */
  function say (f: BayesGraphFinding): string {
    const m = bayesFindingText(f, labelOrNull)
    return te(m.key) ? t(m.key, m.params) : f.code
  }

  /** «a single number», «a number per row», «a number per School», «a number per level of Channel». */
  function typeWords (plate: string): string {
    const d = doc()
    if (!d) return ''
    const w = bayesPlateWords(plate, d)
    switch (w.key) {
      case 'single': return t('bayes.editor.type.single')
      case 'perRow': return t('bayes.editor.type.perRow')
      case 'perUnit': return t('bayes.editor.type.perUnit', { plate: labelOf(w.plate) })
      case 'perLevel': return t('bayes.editor.type.perLevel', { name: labelOf(w.node) })
    }
  }

  /** A slot's name («μ of the prior», «Coefficient of x»). */
  const slotWords = (place: BayesSlotPlace): string => t(`bayes.editor.slot.${place.words.key}`, place.words.params)

  return { labelOf, labelOrNull, say, typeWords, slotWords }
}
