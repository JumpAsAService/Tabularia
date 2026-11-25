<script setup lang="ts">
// The Problems tab (docs/specs/bayes-graph-editor.md, «Mockups», «Components to create»): every
// finding of the mirror in one list, errors first, each with the node or box it is on, the field it
// is in, its message in the person's language, and «Show in the graph», which selects that node on
// the canvas and brings it into sight. Errors are what keeps a model from being saved; warnings
// block nothing.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { CircleAlert, CircleCheck, Crosshair, TriangleAlert } from 'lucide-vue-next'
import { bayesNodeSlots, type BayesDoc } from '~/composables/bayesGraphEdits'
import { bayesProblems } from '~/composables/bayesInspector'
import { useBayesWords } from '~/composables/useBayesWords'
import type { BayesGraphFinding } from '~/composables/bayesGraph'

const props = defineProps<{
  doc: BayesDoc
  findings: BayesGraphFinding[]
  selected: string | null
}>()
const emit = defineEmits<{ (e: 'show', id: string): void }>()
const { t } = useI18n()
const { labelOf, say, slotWords } = useBayesWords(() => props.doc)

const list = computed(() => bayesProblems(props.findings, props.doc))
const errors = computed(() => list.value.filter(p => p.finding.severity === 'error').length)
const warnings = computed(() => list.value.length - errors.value)

const KINDS = new Set(['data', 'parameter', 'transformed', 'linear_predictor', 'likelihood', 'state', 'correlated_effects'])
/** «Mean of the school · Parameter», «School · Group box», or the model itself. */
function whereOf (ownerId: string): { label: string, kind: string } {
  if (!ownerId) return { label: t('bayes.editor.problems.model'), kind: '' }
  const n = props.doc.nodes.find(x => x.id === ownerId)
  if (n) return { label: n.label || n.id, kind: KINDS.has(n.kind) ? t(`bayes.editor.kind.${n.kind}`) : n.kind }
  const p = props.doc.plates.find(x => x.id === ownerId)
  if (p) return { label: p.label || p.id, kind: t(p.kind === 'rows' ? 'bayes.editor.kind.rows' : 'bayes.editor.kind.group') }
  return { label: labelOf(ownerId), kind: '' }
}
/** The field a finding is in, in words («σ of the prior»), or ''. */
function fieldOf (f: BayesGraphFinding, ownerId: string): string {
  if (!f.slot || !ownerId) return ''
  const place = bayesNodeSlots(props.doc, ownerId).find(s => s.node === f.node && s.slot === f.slot)
  return place ? slotWords(place) : ''
}
const exists = (id: string): boolean => props.doc.nodes.some(n => n.id === id) || props.doc.plates.some(p => p.id === id)
</script>

<template>
  <div class="probs">
    <header data-fin="head" class="head">
      <h2>{{ t('bayes.editor.problems.title') }}</h2>
      <p class="count">
        <span v-if="errors" class="e"><CircleAlert :size="13" aria-hidden="true" />{{ t('bayes.editor.problems.errors', { n: errors }) }}</span>
        <span v-if="warnings" class="w"><TriangleAlert :size="13" aria-hidden="true" />{{ t('bayes.editor.problems.warnings', { n: warnings }) }}</span>
      </p>
      <p class="lead">{{ t(errors ? 'bayes.editor.problems.leadErrors' : 'bayes.editor.problems.lead') }}</p>
    </header>

    <div v-if="!list.length" class="none">
      <CircleCheck :size="16" class="ic" aria-hidden="true" />
      <p>{{ t('bayes.editor.problems.none') }}</p>
    </div>

    <ol v-else class="list">
      <li v-for="(p, i) in list" :key="i" class="item" :class="[p.finding.severity === 'error' ? 'e' : 'w', { sel: !!p.owner && p.owner === selected }]">
        <component :is="p.finding.severity === 'error' ? CircleAlert : TriangleAlert" :size="14" class="ic" aria-hidden="true" />
        <div class="body">
          <div class="where">
            <span class="sev">{{ t(p.finding.severity === 'error' ? 'bayes.editor.finding.blocks' : 'bayes.editor.finding.warning') }}</span>
          </div>
          <div class="on">
            <strong>{{ whereOf(p.owner).label }}</strong>
            <span v-if="whereOf(p.owner).kind" class="k">{{ whereOf(p.owner).kind }}</span>
            <span v-if="fieldOf(p.finding, p.owner)" class="k">· {{ fieldOf(p.finding, p.owner) }}</span>
          </div>
          <p class="msg">{{ say(p.finding) }}</p>
          <button
            v-if="p.owner && exists(p.owner)"
            type="button"
            class="mini show"
            :aria-label="t('bayes.editor.problems.showLabel', { label: whereOf(p.owner).label })"
            @click="emit('show', p.owner)"
          >
            <Crosshair :size="13" aria-hidden="true" />{{ t('bayes.editor.problems.show') }}
          </button>
        </div>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.probs { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.head { display: flex; flex-direction: column; gap: 6px; }
.head h2 { margin: 0; font-size: 16px; font-weight: 700; }
.count { display: flex; flex-wrap: wrap; gap: 14px; margin: 0; font-size: 12.5px; }
.count span { display: inline-flex; align-items: center; gap: 5px; }
.count .e { color: var(--danger-text); }
.count .w { color: var(--text); }
.count .w :deep(svg) { color: var(--warning); }
.lead { margin: 0; font-size: 12px; line-height: 1.5; color: var(--muted); max-width: 62ch; }
.none { display: grid; grid-template-columns: 18px 1fr; gap: 8px; padding: 12px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--bg-soft); }
.none .ic { margin-top: 1px; color: var(--accent-2); }
.none p { margin: 0; font-size: 12.5px; line-height: 1.5; }
.list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.item {
  display: grid; grid-template-columns: 16px 1fr; gap: 9px; padding: 10px 12px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--bg-soft); font-size: 12.5px; line-height: 1.45;
}
.item.e { border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); background: var(--tint-danger); }
.item.sel { box-shadow: 0 0 0 1px var(--accent); }
.item > .ic { margin-top: 2px; }
.item.e > .ic { color: var(--danger-text); }
.item.w > .ic { color: var(--warning); }
.body { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.where { font-size: 11.5px; color: var(--muted); }
.on { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; min-width: 0; }
.on strong { font-weight: 600; overflow-wrap: anywhere; }
.on .k { font-size: 11.5px; color: var(--muted); }
.msg { margin: 2px 0 0; overflow-wrap: anywhere; }
.show { align-self: flex-start; margin-top: 6px; display: inline-flex; align-items: center; gap: 5px; }
</style>
