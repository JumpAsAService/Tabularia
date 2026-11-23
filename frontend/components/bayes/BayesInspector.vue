<script setup lang="ts">
// The inspector of the selected node or box (docs/specs/bayes-graph-editor.md, «The inspector»).
// Every node and box: its label, name and box, a box's key and the box it sits in, the rows' series
// (BayesIdentity.vue, lot G1). Then the fields its kind owns (BayesKindFields.vue, lot G2), each
// expression checked by the mirror at every keystroke; the node's problems, the ones on a field said
// under it; its note; and what it is, for a reader who does not know Bayes.
//
// A parameter, a latent state and correlated effects have charts of their priors (prior-preview):
// beside the numbers in a panel of 600 px, under them in a narrow one; a card for each hyperprior the
// arguments read. A computed value that needs no data has the chart of its values.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { AudioWaveform, Boxes, ChartScatter, CircleAlert, CircleDot, Database, Info, Rows3, Sigma, SquareFunction, TriangleAlert, Variable } from 'lucide-vue-next'
import BayesIdentity from '~/components/bayes/BayesIdentity.vue'
import BayesKindFields from '~/components/bayes/BayesKindFields.vue'
import BayesPriorChart from '~/components/bayes/BayesPriorChart.vue'
import { bayesArgSymbol } from '~/composables/bayesEditorText'
import { bayesNodeSlots, bayesSetNote, type BayesDoc } from '~/composables/bayesGraphEdits'
import { bayesSlotView, type BayesSlotView } from '~/composables/bayesInspector'
import { useBayesWords } from '~/composables/useBayesWords'
import type { BayesGraphFinding, BayesModel, BayesPriorPlan } from '~/composables/bayesGraph'
import type { BayesTemplateNode } from '~/composables/bayesTemplates'

const props = defineProps<{
  doc: BayesDoc
  model: BayesModel | null
  findings: BayesGraphFinding[]
  plan: BayesPriorPlan | null
  selected: string | null
  /** The template's explanation of a node while it is as the template wrote it, or undefined. */
  why?: (id: string) => string | undefined
}>()
const emit = defineEmits<{
  (e: 'change', fn: (d: BayesDoc) => unknown, step: boolean): void
  (e: 'form', plate: string, nonCentred: boolean): void
  (e: 'select', id: string): void
  (e: 'escape'): void
  (e: 'label', id: string, text: string): void
  (e: 'name', id: string, text: string): void
  (e: 'plate', id: string, plate: string | null): void
  (e: 'parent', id: string, parent: string | null): void
  (e: 'key', id: string, key: string): void
  (e: 'target', id: string, target: string): void
  (e: 'series', on: boolean): void
  (e: 'seriesPart', part: 'time' | 'by', value: string | null): void
}>()
// the identity's events go up as they are
const ident = {
  label: (id: string, v: string) => emit('label', id, v),
  name: (id: string, v: string) => emit('name', id, v),
  plate: (id: string, v: string | null) => emit('plate', id, v),
  parent: (id: string, v: string | null) => emit('parent', id, v),
  key: (id: string, v: string) => emit('key', id, v),
  target: (id: string, v: string) => emit('target', id, v),
  series: (on: boolean) => emit('series', on),
  seriesPart: (part: 'time' | 'by', v: string | null) => emit('seriesPart', part, v),
  escape: () => emit('escape'),
}
const { t, te } = useI18n()
const words = useBayesWords(() => props.doc)
const { labelOf, say, typeWords, slotWords } = words

const owner = (id: string): string => { const i = id.indexOf('.'); return i < 0 ? id : id.slice(0, i) }
const nodeById = computed(() => new Map(props.doc.nodes.map(n => [n.id, n])))
const node = computed<BayesTemplateNode | undefined>(() => (props.selected ? nodeById.value.get(props.selected) : undefined))
const plate = computed(() => (props.selected ? props.doc.plates.find(p => p.id === props.selected) : undefined))
const own = computed(() => props.findings.filter(f => props.selected !== null && owner(f.node) === props.selected))

const KIND_ICONS: Record<string, unknown> = {
  data: Database, parameter: CircleDot, transformed: SquareFunction, linear_predictor: Sigma,
  likelihood: ChartScatter, state: AudioWaveform, correlated_effects: Variable,
}

// ---------------------------------------------------------------- the node's slots

const views = computed(() => {
  const m = new Map<string, BayesSlotView>()
  if (!node.value) return m
  for (const place of bayesNodeSlots(props.doc, node.value.id)) {
    if (place.path === 'terms.new') continue
    m.set(place.path, bayesSlotView(place, props.findings, props.model))
  }
  return m
})
/** The field a finding is said under, when the inspector shows one for its slot. */
function fieldOf (f: BayesGraphFinding): BayesSlotView | undefined {
  for (const v of views.value.values()) if (v.place.node === f.node && v.place.slot === f.slot) return v
  return undefined
}
const fieldWords = (v: BayesSlotView): string => slotWords(v.place)
const ownSorted = computed(() => [...own.value].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === 'error' ? -1 : 1)))

// ---------------------------------------------------------------- the charts

/** The slots whose prior a chart draws, by kind: a parameter's, a state's first value's. */
const chartPrefix = computed(() => (node.value?.kind === 'parameter' ? 'prior.' : node.value?.kind === 'state' ? 'init.' : ''))
const chartViews = computed(() => (chartPrefix.value ? [...views.value.values()].filter(v => v.place.path.startsWith(chartPrefix.value)) : []))
const staleArg = computed(() => {
  const v = chartViews.value.find(x => x.error)
  return v ? bayesArgSymbol(v.place.path.slice(chartPrefix.value.length)) : null
})
// the node's other errors (a name, a bound…) stop the chart too; the arguments' are said in place
const otherErrors = computed(() => own.value.some(f => f.severity === 'error' && !chartViews.value.some(v => v.place.node === f.node && v.place.slot === f.slot)))
// every parameter the prior's arguments read, once: each gets the card of its own prior
const hypers = computed(() => {
  const out: string[] = []
  for (const v of chartViews.value) for (const id of v.refs) if (!out.includes(id) && nodeById.value.get(id)?.kind === 'parameter') out.push(id)
  return out.slice(0, 3)
})
const chartTitle = computed(() => {
  const n = node.value
  if (!n) return ''
  if (n.kind === 'state') return t('bayes.editor.chart.firstValue')
  const p = typeof n.plate === 'string' ? n.plate : ''
  return p ? t('bayes.editor.chart.titlePer', { plate: labelOf(p) }) : t('bayes.editor.chart.title')
})
/**
 * Two columns (numbers, chart) for a parameter and a state, whose chart is their point; correlated
 * effects keep their fields at the panel's whole width and put the charts of their effects and of
 * their correlation in a row of cards under them.
 */
const twoCols = computed(() => ['parameter', 'state'].includes(node.value?.kind ?? ''))

// ---------------------------------------------------------------- what it is

const explainKey = computed(() => {
  const n = node.value
  if (!n) return plate.value ? `bayes.editor.explain.${plate.value.kind === 'rows' ? 'rows' : 'group'}` : ''
  return n.kind === 'data' ? `bayes.editor.explain.data_${String(n.type ?? 'real')}` : `bayes.editor.explain.${n.kind}`
})
const explanation = computed(() => {
  const n = node.value
  const fromTemplate = n && props.why ? props.why(n.id) : undefined
  if (fromTemplate) return [fromTemplate]
  const out: string[] = []
  if (explainKey.value && te(explainKey.value)) out.push(t(explainKey.value))
  const dist = n?.kind === 'parameter' ? n.prior?.dist : n?.kind === 'likelihood' ? String(n.dist ?? '') : n?.kind === 'state' ? n.init?.dist : ''
  if (dist && te(`bayes.dist.${dist}.what`)) out.push(`${t(`bayes.dist.${dist}.name`)}: ${t(`bayes.dist.${dist}.what`)}`)
  return out
})

// a change of the node's own fields goes up to the page as it is
const onChange = (fn: (d: BayesDoc) => unknown, step: boolean) => emit('change', fn, step)

// ---------------------------------------------------------------- the note

const holder = computed(() => node.value ?? plate.value)
const noteFindings = computed(() => own.value.filter(f => f.code === 'graph.note' && f.node === props.selected))
const setNote = (e: Event) => {
  const id = props.selected
  const text = (e.target as HTMLTextAreaElement).value
  if (id) emit('change', d => bayesSetNote(d, id, text), false)
}

// ---------------------------------------------------------------- a box

const plateNodes = computed(() => {
  const p = plate.value
  if (!p) return []
  return props.doc.nodes.filter(n => n.plate === p.id || (n.kind === 'likelihood' && nodeById.value.get(String(n.observed ?? ''))?.plate === p.id))
})
// the boxes nested in it
const plateBoxes = computed(() => (plate.value ? props.doc.plates.filter(x => x.kind === 'group' && x.parent === plate.value!.id) : []))
const plateFindings = computed(() => {
  const p = plate.value
  if (!p) return []
  const ids = new Set([p.id, ...plateNodes.value.map(n => n.id)])
  return props.findings.filter(f => ids.has(owner(f.node)))
})
const nonCentred = computed(() => (plate.value?.form ?? 'non_centred') !== 'centred')
</script>

<template>
  <div class="insp">
    <!-- nothing selected -->
    <div v-if="!node && !plate" class="empty">
      <p>{{ t('bayes.editor.inspector.empty') }}</p>
      <p class="m">{{ t('bayes.editor.inspector.emptyKeys') }}</p>
    </div>

    <!-- a box -->
    <template v-else-if="plate">
      <div class="kindline"><component :is="plate.kind === 'rows' ? Rows3 : Boxes" :size="13" aria-hidden="true" />{{ t(plate.kind === 'rows' ? 'bayes.editor.kind.rows' : 'bayes.editor.kind.group') }}</div>
      <h2 class="ptitle">{{ plate.label || '…' }}</h2>
      <BayesIdentity :doc="doc" :id="plate.id" :findings="findings" v-on="ident" />
      <section v-if="plate.kind === 'group'" class="sec">
        <h3>{{ t('bayes.editor.inspector.form') }}</h3>
        <label class="swrow">
          <input type="checkbox" role="switch" :checked="nonCentred" @change="emit('form', plate.id, ($event.target as HTMLInputElement).checked)">
          {{ t('bayes.editor.plate.nonCentred') }}
        </label>
        <p class="explain">{{ t(nonCentred ? 'bayes.editor.plate.nonCentredWhat' : 'bayes.editor.plate.centredWhat') }}</p>
        <p class="explain m">{{ t('bayes.editor.plate.boundedStays') }}</p>
      </section>
      <section class="sec">
        <h3>{{ t('bayes.editor.inspector.contains') }}</h3>
        <p v-if="!plateBoxes.length && !plateNodes.length" class="explain m">{{ t('bayes.editor.inspector.containsNothing') }}</p>
        <ul class="members">
          <li v-for="b in plateBoxes" :key="b.id">
            <button type="button" class="member" @click="emit('select', b.id)">
              <Boxes :size="13" aria-hidden="true" />
              <span class="nm">{{ b.label }}</span>
              <span class="k">{{ t('bayes.editor.kind.group') }}</span>
            </button>
          </li>
          <li v-for="n in plateNodes" :key="n.id">
            <button type="button" class="member" @click="emit('select', n.id)">
              <component :is="KIND_ICONS[n.kind] ?? CircleDot" :size="13" aria-hidden="true" />
              <span class="nm">{{ n.label }}</span>
              <code v-if="n.name">{{ n.name }}</code>
              <span class="k">{{ t(`bayes.editor.kind.${n.kind}`) }}</span>
            </button>
          </li>
        </ul>
        <p v-if="plate.kind === 'group'" class="explain m">{{ t('bayes.editor.inspector.unitsFromData', { key: String(doc.nodes.find(n => n.id === plate!.key)?.name ?? '…') }) }}</p>
      </section>
      <section class="sec">
        <h3>{{ t('bayes.editor.inspector.problemsOfBox') }}</h3>
        <p v-if="!plateFindings.length" class="explain m">{{ t('bayes.editor.inspector.noProblems') }}</p>
        <div v-for="(f, i) in plateFindings" :key="i" class="finding" :class="f.severity === 'error' ? 'e' : 'w'">
          <component :is="f.severity === 'error' ? CircleAlert : TriangleAlert" :size="14" class="ic" aria-hidden="true" />
          <div>
            <div class="where">{{ t(f.severity === 'error' ? 'bayes.editor.finding.error' : 'bayes.editor.finding.warning') }} · {{ labelOf(owner(f.node)) }}</div>
            {{ say(f) }}
          </div>
        </div>
      </section>
      <section class="sec">
        <h3>{{ t('bayes.editor.inspector.what') }}</h3>
        <p v-for="(x, i) in explanation" :key="i" class="explain">{{ x }}</p>
      </section>
      <section class="sec">
        <h3><label :for="`bi-${plate.id}-note`">{{ t('bayes.editor.inspector.note') }}</label></h3>
        <textarea :id="`bi-${plate.id}-note`" class="nokey note" rows="2" :value="String(plate.note ?? '')" :placeholder="t('bayes.editor.inspector.notePlaceholder')" @input="setNote" @keydown.esc="emit('escape')" />
        <p v-for="(f, i) in noteFindings" :key="i" class="errline">{{ say(f) }}</p>
      </section>
    </template>

    <!-- a node: its fields, and for the kinds with a prior the charts beside them -->
    <div v-else-if="node" :class="twoCols ? 'cols' : 'one'">
      <div class="col">
        <div class="kindline">
          <component :is="KIND_ICONS[node.kind] ?? CircleDot" :size="13" aria-hidden="true" />
          {{ node.plate ? t('bayes.editor.inspector.kindInBox', { kind: t(`bayes.editor.kind.${node.kind}`), box: labelOf(String(node.plate)) }) : t(`bayes.editor.kind.${node.kind}`) }}
        </div>
        <h2 class="ptitle">{{ node.label || '…' }}</h2>
        <BayesIdentity :doc="doc" :id="node.id" :findings="findings" v-on="ident" />
        <BayesKindFields
          :doc="doc"
          :node="node"
          :model="model"
          :findings="findings"
          :views="views"
          :say="say"
          :type-words="typeWords"
          :label-of="labelOf"
          @change="onChange"
          @escape="emit('escape')"
        />
        <section class="sec">
          <h3>{{ t('bayes.editor.inspector.problems') }}</h3>
          <p v-if="!own.length" class="explain m">{{ t('bayes.editor.inspector.noProblems') }}</p>
          <div v-for="(f, i) in ownSorted" :key="i" class="finding" :class="f.severity === 'error' ? 'e' : 'w'">
            <component :is="f.severity === 'error' ? CircleAlert : TriangleAlert" :size="14" class="ic" aria-hidden="true" />
            <div>
              <div class="where">{{ t(f.severity === 'error' ? 'bayes.editor.finding.blocks' : 'bayes.editor.finding.warning') }}</div>
              <template v-if="fieldOf(f) && (fieldOf(f)!.error === f || fieldOf(f)!.warning === f)">{{ t('bayes.editor.finding.underSlot', { slot: fieldWords(fieldOf(f)!) }) }}</template>
              <template v-else>{{ say(f) }}</template>
            </div>
          </div>
        </section>
        <section v-if="!twoCols" class="sec">
          <h3>{{ t('bayes.editor.inspector.what') }}</h3>
          <p v-for="(x, i) in explanation" :key="i" class="explain">{{ x }}</p>
        </section>
        <section v-if="node.kind === 'correlated_effects'" class="sec">
          <h3>{{ t('bayes.editor.chart.effects') }}</h3>
          <div class="cardgrid">
            <div v-for="c in node.components ?? []" :key="c.id" class="hyper">
              <div class="hh"><Variable :size="13" aria-hidden="true" /><span>{{ t('bayes.editor.chart.component', { name: c.name || c.label }) }}</span></div>
              <BayesPriorChart :plan="plan" :target="`${node.id}.${c.id}`" :name="c.label" compact :label-of="labelOf" />
            </div>
            <div v-if="node.correlation" class="hyper">
              <div class="hh"><Variable :size="13" aria-hidden="true" /><span>{{ t('bayes.editor.chart.correlation', { name: node.correlation.name || node.correlation.label }) }}</span></div>
              <BayesPriorChart :plan="plan" :target="`${node.id}.correlation`" :name="node.correlation.label" compact :label-of="labelOf" />
            </div>
          </div>
        </section>
        <section v-if="node.kind === 'transformed'" class="sec">
          <BayesPriorChart :plan="plan" :target="node.id" :name="node.label" :code="String(node.name ?? node.id)" :title="t('bayes.editor.chart.values')" :blocked="own.some(f => f.severity === 'error')" :label-of="labelOf" />
        </section>
        <section class="sec">
          <h3><label :for="`bi-${node.id}-note`">{{ t('bayes.editor.inspector.note') }}</label></h3>
          <textarea :id="`bi-${node.id}-note`" class="nokey note" rows="2" :value="String(node.note ?? '')" :placeholder="t('bayes.editor.inspector.notePlaceholder')" @input="setNote" @keydown.esc="emit('escape')" />
          <p v-for="(f, i) in noteFindings" :key="i" class="errline">{{ say(f) }}</p>
        </section>
      </div>
      <div v-if="twoCols" class="col">
        <BayesPriorChart
          :plan="plan"
          :target="node.kind === 'state' ? `${node.id}.init` : node.id"
          :name="node.kind === 'state' ? t('bayes.editor.chart.firstValueOf', { label: node.label }) : node.label"
          :code="String(node.name ?? node.id)"
          :title="chartTitle"
          :stale-arg="staleArg"
          :blocked="otherErrors"
          :label-of="labelOf"
        />
        <div v-for="h in hypers" :key="h" class="hyper">
          <div class="hh">
            <CircleDot :size="13" aria-hidden="true" />
            <span>{{ t('bayes.editor.chart.hyper', { name: nodeById.get(h)?.name ?? h }) }}</span>
            <button type="button" class="link" @click="emit('select', h)">{{ t('bayes.editor.chart.open') }}</button>
          </div>
          <BayesPriorChart :plan="plan" :target="h" :name="labelOf(h)" compact :label-of="labelOf" />
        </div>
        <section class="sec">
          <h3><Info :size="12" aria-hidden="true" />{{ t('bayes.editor.inspector.what') }}</h3>
          <p v-for="(x, i) in explanation" :key="i" class="explain">{{ x }}</p>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.insp { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.empty p { margin: 0 0 8px; font-size: 12.5px; line-height: 1.5; color: var(--text); }
.empty p.m, .m { color: var(--muted); }
.kindline { display: flex; align-items: center; gap: 6px; font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.ptitle { margin: -8px 0 0; font-size: 16px; font-weight: 700; line-height: 1.3; overflow-wrap: anywhere; }
.cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 18px; align-items: start; }
.one { display: flex; flex-direction: column; min-width: 0; }
.col { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.col .ptitle { margin-top: -6px; }
.sec { display: flex; flex-direction: column; gap: 10px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
.sec h3 { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.sec h3 label { cursor: inherit; }
.explain { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--text); }
.explain.m { color: var(--muted); font-size: 12px; }
.errline { margin: 0; font-size: 12px; color: var(--danger-text); }
.swrow { display: flex; align-items: center; gap: 9px; font-size: 13px; cursor: pointer; }
.note { min-height: 52px; resize: vertical; font-family: inherit; font-size: 12.5px; line-height: 1.45; }
.finding {
  display: grid; grid-template-columns: 16px 1fr; gap: 8px; padding: 8px 10px; border-radius: 8px;
  font-size: 12.5px; line-height: 1.45; border: 1px solid var(--border); background: var(--bg-soft);
}
.finding.e { border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); background: var(--tint-danger); }
.finding.e > .ic { margin-top: 2px; color: var(--danger-text); }
.finding.w > .ic { margin-top: 2px; color: var(--warning); }
.finding .where { margin-bottom: 2px; font-size: 11.5px; color: var(--muted); }
.members { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.members:empty { display: none; }
.member {
  width: 100%; display: flex; align-items: center; gap: 8px; padding: 6px 10px; text-align: left;
  border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); color: var(--text); font-size: 12.5px;
}
.member .nm { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.member code { font-size: 11.5px; color: var(--muted); }
.member .k { margin-left: auto; font-size: 11.5px; color: var(--muted); white-space: nowrap; }
@media (hover: hover) and (pointer: fine) { .member:hover { border-color: var(--accent); } }
.cardgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
.hyper { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border-soft); background: var(--bg-soft); }
.hh { display: flex; align-items: center; gap: 6px; font-size: 12.5px; min-width: 0; }
.hh span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hh .link { margin-left: auto; padding: 0; border: 0; background: none; color: var(--muted); font-size: 11.5px; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
@media (hover: hover) and (pointer: fine) { .hh .link:hover { color: var(--accent); } }
/* the charts stand beside the numbers in a panel of 600 px, under them in a narrower one (the page's
   side panel is the container: its width follows the window and the person's handle) */
@container bayes-panel (max-width: 540px) { .cols { grid-template-columns: minmax(0, 1fr); } }
</style>
