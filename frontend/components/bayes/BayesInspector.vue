<script setup lang="ts">
// The inspector of the selected node or box (docs/specs/bayes-graph-editor.md, «The inspector»).
// Lot G0 edits a parameter's prior numbers only: its arguments, checked by the mirror at every
// keystroke, and its bounds; the chart of the prior stands beside the numbers (under them in a
// narrow panel), with a card for each hyperprior its arguments read. The other kinds and the boxes
// show what they are and their problems; their own fields come with lot G2.
import { computed, reactive, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { AudioWaveform, Boxes, ChartScatter, CircleAlert, CircleDot, Database, Info, Rows3, Sigma, SquareFunction, TriangleAlert, Variable } from 'lucide-vue-next'
import BayesExprField from '~/components/bayes/BayesExprField.vue'
import BayesPriorChart from '~/components/bayes/BayesPriorChart.vue'
import { bayesArgSymbol, bayesConstraintKey, bayesFindingText, bayesPriorText } from '~/composables/bayesEditorText'
import { bayesDistribution } from '~/composables/bayesCatalog'
import { bayesSlotKey, type BayesGraphFinding, type BayesModel, type BayesPriorPlan } from '~/composables/bayesGraph'
import type { BayesTemplateGraph, BayesTemplateNode, BayesTemplateSlot } from '~/composables/bayesTemplates'

const props = defineProps<{
  doc: BayesTemplateGraph
  model: BayesModel | null
  findings: BayesGraphFinding[]
  plan: BayesPriorPlan | null
  selected: string | null
}>()
const emit = defineEmits<{
  (e: 'arg', node: string, arg: string, text: string): void
  (e: 'bound', node: string, which: 'lower' | 'upper', value: number | null): void
  (e: 'form', plate: string, nonCentred: boolean): void
  (e: 'select', id: string): void
  (e: 'escape'): void
}>()
const { t, te, locale } = useI18n()

const owner = (id: string): string => { const i = id.indexOf('.'); return i < 0 ? id : id.slice(0, i) }
const nodeById = computed(() => new Map(props.doc.nodes.map(n => [n.id, n])))
const plateById = computed(() => new Map(props.doc.plates.map(p => [p.id, p])))
function labelOf (id: string): string {
  return nodeById.value.get(id)?.label ?? plateById.value.get(id)?.label ?? id
}
const say = (f: BayesGraphFinding): string => {
  const m = bayesFindingText(f, labelOf)
  return te(m.key) ? t(m.key, m.params) : f.code
}
const num = (v: number): string => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 6 }).format(v)

const node = computed<BayesTemplateNode | undefined>(() => (props.selected ? nodeById.value.get(props.selected) : undefined))
const plate = computed(() => (props.selected ? plateById.value.get(props.selected) : undefined))
const own = computed(() => props.findings.filter(f => props.selected !== null && owner(f.node) === props.selected))

/** «School, inside District»: a box and the boxes it is nested in. */
function boxWords (id: string | null | undefined): string {
  if (!id) return t('bayes.editor.inspector.noBox')
  const p = plateById.value.get(id)
  if (!p) return id
  const parent = p.parent ? plateById.value.get(String(p.parent)) : undefined
  return parent ? t('bayes.editor.inspector.boxInside', { box: p.label, parent: parent.label }) : p.label
}

const KIND_ICONS: Record<string, unknown> = {
  data: Database, parameter: CircleDot, transformed: SquareFunction, linear_predictor: Sigma,
  likelihood: ChartScatter, state: AudioWaveform, correlated_effects: Variable,
}

// ---------------------------------------------------------------- a parameter's prior

const dist = computed(() => (node.value?.prior ? bayesDistribution(node.value.prior.dist) : undefined))
const args = computed(() => {
  const n = node.value
  const d = dist.value
  if (!n?.prior || !d) return []
  return d.params.map(p => {
    const slot = `prior.${p.name}`
    const text = n.prior!.args[p.name]?.expr ?? n.prior!.args[p.name]?.ref ?? ''
    const fs = own.value.filter(f => f.node === n.id && f.slot === slot && f.severity === 'error')
    const first = fs[0]
    const a = (first?.args ?? {}) as Record<string, unknown>
    const error = first
      ? (typeof a.pos === 'number'
          ? { pos: a.pos, len: typeof a.len === 'number' ? a.len : 0, message: say(first) }
          : { pos: 0, len: Array.from(text).length, message: say(first) })
      : null
    const typed = props.model?.slots.get(bayesSlotKey(n.id, slot))
    let okText = ''
    if (!error && typed) {
      okText = typed.type.plate ? t('bayes.editor.type.perUnit', { plate: labelOf(typed.type.plate) }) : t('bayes.editor.type.single')
    }
    const refs = (typed?.refs ?? []).filter(id => nodeById.value.get(owner(id))?.kind === 'parameter')
    const hyper = !error && refs.length === 1 && props.model?.params.get(owner(refs[0]!))?.role === 'hyperprior' ? owner(refs[0]!) : ''
    const constraint = bayesConstraintKey(p.constraint)
    return {
      name: p.name,
      symbol: bayesArgSymbol(p.name),
      words: te(`bayes.dist.${d.id}.param.${p.name}`) ? t(`bayes.dist.${d.id}.param.${p.name}`) : p.name,
      constraint: constraint ? t(constraint) : '',
      text,
      error,
      okText,
      hyper,
      refs,
    }
  })
})
const staleArg = computed(() => args.value.find(a => a.error)?.symbol ?? null)
// the node's other errors (a name, a bound…) stop the chart too; the arguments' are said in place
const otherErrors = computed(() => own.value.some(f => f.severity === 'error' && !args.value.some(a => `prior.${a.name}` === f.slot)))
// every parameter a prior's argument reads, once: each gets the card of its own prior
const hypers = computed(() => {
  const out: string[] = []
  for (const a of args.value) for (const id of a.refs) if (!out.includes(owner(id))) out.push(owner(id))
  return out.slice(0, 3)
})
const bounds = computed(() => (node.value?.bounds ?? {}) as { lower?: number | null, upper?: number | null })
const param = computed(() => (node.value ? props.model?.params.get(node.value.id) : undefined))
const chartTitle = computed(() => {
  const p = node.value?.plate
  return p ? t('bayes.editor.chart.titlePer', { plate: labelOf(String(p)) }) : t('bayes.editor.chart.title')
})

// a bound that is not a number is not written into the graph: it is said under its field
const badBound = reactive({ lower: false, upper: false })
watch(() => props.selected, () => { badBound.lower = false; badBound.upper = false })
function onBound (which: 'lower' | 'upper', e: Event) {
  const raw = (e.target as HTMLInputElement).value.trim().replace(',', '.')
  if (!node.value) return
  const v = Number(raw)
  badBound[which] = raw !== '' && !Number.isFinite(v)
  if (raw === '') emit('bound', node.value.id, which, null)
  else if (!badBound[which]) emit('bound', node.value.id, which, v)
}

// ---------------------------------------------------------------- the other kinds

const slotText = (s: BayesTemplateSlot | undefined): string => (s?.expr ?? s?.ref ?? '').trim()
const nodeLines = computed(() => {
  const n = node.value
  if (!n) return []
  const nameOf = (id: string): string => String(nodeById.value.get(id)?.name ?? id)
  switch (n.kind) {
    case 'transformed': return [slotText(n.value as BayesTemplateSlot)]
    case 'linear_predictor': {
      const terms = ((n.terms ?? []) as Array<{ data: string, coef: BayesTemplateSlot }>).map(x => `${slotText(x.coef)} × ${nameOf(x.data)}`)
      return [[slotText(n.intercept as BayesTemplateSlot), ...terms].filter(Boolean).join(' + ')]
    }
    case 'likelihood': {
      const order = bayesDistribution(String(n.dist))?.params.map(x => x.name) ?? []
      return [`${nameOf(String(n.observed ?? ''))} ~ ${bayesPriorText(String(n.dist), n.args ?? {}, order)}`]
    }
    case 'state': {
      const init = n.init
      const order = init ? bayesDistribution(init.dist)?.params.map(x => x.name) ?? [] : []
      return [init ? bayesPriorText(init.dist, init.args, order) : '', slotText(n.scale as BayesTemplateSlot)].filter(Boolean)
    }
    case 'correlated_effects':
      return (n.components ?? []).map(c => `${c.name} ~ normal(${slotText(c.mean)}, ${slotText(c.scale)})`)
    default: return []
  }
})
const drawnPlate = computed(() => {
  const n = node.value
  if (!n) return null
  if (n.kind === 'likelihood') return (nodeById.value.get(String(n.observed ?? ''))?.plate as string | null | undefined) ?? null
  return (n.plate as string | null | undefined) ?? null
})

// ---------------------------------------------------------------- a box

const plateNodes = computed(() => {
  const p = plate.value
  if (!p) return []
  return props.doc.nodes.filter(n => n.plate === p.id || (n.kind === 'likelihood' && nodeById.value.get(String(n.observed ?? ''))?.plate === p.id))
})
const plateFindings = computed(() => {
  const p = plate.value
  if (!p) return []
  const ids = new Set([p.id, ...plateNodes.value.map(n => n.id)])
  return props.findings.filter(f => ids.has(owner(f.node)))
})
const keyNode = computed(() => (plate.value?.key ? nodeById.value.get(String(plate.value.key)) : undefined))
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
      <h2 class="ptitle">{{ plate.label }}</h2>
      <div v-if="plate.kind === 'group'" class="row2">
        <div class="fact">
          <span class="lbl">{{ t('bayes.editor.inspector.unitPer') }}</span>
          <span class="val"><code>{{ keyNode?.name ?? plate.key }}</code> <span class="m">{{ keyNode?.label }}</span></span>
        </div>
        <div class="fact">
          <span class="lbl">{{ t('bayes.editor.inspector.inside') }}</span>
          <span class="val">{{ plate.parent ? labelOf(String(plate.parent)) : t('bayes.editor.inspector.noBox') }}</span>
        </div>
      </div>
      <p v-else class="explain">{{ t('bayes.editor.plate.rowsWhat') }}</p>
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
        <ul class="members">
          <li v-for="n in plateNodes" :key="n.id">
            <button type="button" class="member" @click="emit('select', n.id)">
              <component :is="KIND_ICONS[n.kind] ?? CircleDot" :size="13" aria-hidden="true" />
              <span class="nm">{{ n.label }}</span>
              <code v-if="n.name">{{ n.name }}</code>
              <span class="k">{{ t(`bayes.editor.kind.${n.kind}`) }}</span>
            </button>
          </li>
        </ul>
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
    </template>

    <!-- a parameter: its prior's numbers and the chart beside them -->
    <div v-else-if="node && node.kind === 'parameter'" class="cols">
      <div class="col">
        <div class="kindline">
          <CircleDot :size="13" aria-hidden="true" />
          {{ node.plate ? t('bayes.editor.inspector.kindInBox', { kind: t('bayes.editor.kind.parameter'), box: labelOf(String(node.plate)) }) : t('bayes.editor.kind.parameter') }}
        </div>
        <h2 class="ptitle">{{ node.label }}</h2>
        <div class="row2">
          <div class="fact">
            <span class="lbl">{{ t('bayes.editor.inspector.name') }}</span>
            <code class="val">{{ node.name }}</code>
          </div>
          <div class="fact">
            <span class="lbl">{{ t('bayes.editor.inspector.box') }}</span>
            <span class="val">{{ boxWords(node.plate as string | null) }}</span>
          </div>
        </div>
        <section class="sec">
          <h3>{{ t('bayes.editor.inspector.prior') }}</h3>
          <div class="fact">
            <span class="lbl">{{ t('bayes.editor.inspector.distribution') }}</span>
            <span class="val">{{ dist ? t(`bayes.dist.${dist.id}.name`) : node.prior?.dist }}</span>
            <span v-if="dist" class="hint">{{ t(`bayes.dist.${dist.id}.what`) }}</span>
          </div>
          <div v-for="a in args" :key="a.name" class="fl">
            <label :for="`arg-${node.id}-${a.name}`" class="argl">
              <span class="sym">{{ a.symbol }}</span>{{ a.words }}
              <span v-if="a.constraint" class="c">{{ a.constraint }}</span>
            </label>
            <BayesExprField
              :id="`arg-${node.id}-${a.name}`"
              :model-value="a.text"
              :error="a.error"
              :ok-text="a.okText"
              :tag="a.hyper ? t('bayes.editor.node.hyperprior') : ''"
              @update:model-value="(v: string) => emit('arg', node!.id, a.name, v)"
              @escape="emit('escape')"
            />
          </div>
          <div class="row2">
            <div class="fl">
              <label :for="`lo-${node.id}`">{{ t('bayes.editor.bounds.lower') }}</label>
              <input
                :id="`lo-${node.id}`"
                class="nokey"
                type="text"
                inputmode="decimal"
                :value="bounds.lower ?? ''"
                :placeholder="t('bayes.editor.bounds.none')"
                :aria-invalid="badBound.lower ? 'true' : 'false'"
                :aria-describedby="badBound.lower ? `lo-${node.id}-err` : undefined"
                @input="onBound('lower', $event)"
                @keydown.esc="emit('escape')"
              >
              <p v-if="badBound.lower" :id="`lo-${node.id}-err`" class="errline">{{ t('bayes.editor.bounds.notNumber') }}</p>
            </div>
            <div class="fl">
              <label :for="`hi-${node.id}`">{{ t('bayes.editor.bounds.upper') }}</label>
              <input
                :id="`hi-${node.id}`"
                class="nokey"
                type="text"
                inputmode="decimal"
                :value="bounds.upper ?? ''"
                :placeholder="t('bayes.editor.bounds.none')"
                :aria-invalid="badBound.upper ? 'true' : 'false'"
                :aria-describedby="badBound.upper ? `hi-${node.id}-err` : undefined"
                @input="onBound('upper', $event)"
                @keydown.esc="emit('escape')"
              >
              <p v-if="badBound.upper" :id="`hi-${node.id}-err`" class="errline">{{ t('bayes.editor.bounds.notNumber') }}</p>
            </div>
          </div>
          <p v-if="param?.truncated" class="explain">{{ t('bayes.editor.bounds.cut', { range: [bounds.lower != null ? `≥ ${num(bounds.lower)}` : '', bounds.upper != null ? `≤ ${num(bounds.upper)}` : ''].filter(Boolean).join(', ') }) }}</p>
        </section>
        <section class="sec">
          <h3>{{ t('bayes.editor.inspector.problems') }}</h3>
          <p v-if="!own.length" class="explain m">{{ t('bayes.editor.inspector.noProblems') }}</p>
          <div v-for="(f, i) in own" :key="i" class="finding" :class="f.severity === 'error' ? 'e' : 'w'">
            <component :is="f.severity === 'error' ? CircleAlert : TriangleAlert" :size="14" class="ic" aria-hidden="true" />
            <div>
              <div class="where">{{ t(f.severity === 'error' ? 'bayes.editor.finding.blocks' : 'bayes.editor.finding.warning') }}</div>
              <template v-if="f.slot.startsWith('prior.') && args.some(a => `prior.${a.name}` === f.slot)">
                {{ t('bayes.editor.finding.underField', { arg: bayesArgSymbol(f.slot.slice(6)) }) }}
              </template>
              <template v-else>{{ say(f) }}</template>
            </div>
          </div>
        </section>
      </div>
      <div class="col">
        <BayesPriorChart
          :plan="plan"
          :target="node.id"
          :name="node.label"
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
      </div>
    </div>

    <!-- any other node: what it is, read only in this lot -->
    <template v-else-if="node">
      <div class="kindline"><component :is="KIND_ICONS[node.kind] ?? CircleDot" :size="13" aria-hidden="true" />{{ t(`bayes.editor.kind.${node.kind}`) }}</div>
      <h2 class="ptitle">{{ node.label }}</h2>
      <div class="row2">
        <div class="fact">
          <span class="lbl">{{ t('bayes.editor.inspector.name') }}</span>
          <code class="val">{{ node.name || '—' }}</code>
        </div>
        <div class="fact">
          <span class="lbl">{{ t('bayes.editor.inspector.box') }}</span>
          <span class="val">{{ boxWords(drawnPlate) }}</span>
        </div>
      </div>
      <pre v-for="(l, i) in nodeLines" :key="i" class="line">{{ l }}</pre>
      <p class="explain m"><Info :size="12" aria-hidden="true" /> {{ t('bayes.editor.inspector.readOnly') }}</p>
      <section class="sec">
        <h3>{{ t('bayes.editor.inspector.problems') }}</h3>
        <p v-if="!own.length" class="explain m">{{ t('bayes.editor.inspector.noProblems') }}</p>
        <div v-for="(f, i) in own" :key="i" class="finding" :class="f.severity === 'error' ? 'e' : 'w'">
          <component :is="f.severity === 'error' ? CircleAlert : TriangleAlert" :size="14" class="ic" aria-hidden="true" />
          <div>
            <div class="where">{{ t(f.severity === 'error' ? 'bayes.editor.finding.blocks' : 'bayes.editor.finding.warning') }}</div>
            {{ say(f) }}
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.insp { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.empty p { margin: 0 0 8px; font-size: 12.5px; line-height: 1.5; color: var(--text); }
.empty p.m, .m { color: var(--muted); }
.kindline { display: flex; align-items: center; gap: 6px; font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.ptitle { margin: -8px 0 0; font-size: 16px; font-weight: 700; line-height: 1.3; }
.cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 18px; align-items: start; }
.col { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.col .ptitle { margin-top: -6px; }
.row2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
.fact, .fl { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.fact .lbl, .fl > label { font-size: 12px; font-weight: 500; color: var(--muted); }
.fact .val { font-size: 13px; color: var(--text); overflow-wrap: anywhere; }
.fact code.val { font-size: 12.5px; }
.fact .hint { font-size: 12px; line-height: 1.4; color: var(--muted); }
.argl { display: flex; align-items: baseline; gap: 6px; font-size: 12px; font-weight: 500; color: var(--muted); }
.argl .sym { color: var(--text); font-weight: 600; }
.argl .c { margin-left: auto; font-weight: 400; font-size: 11.5px; }
.sec { display: flex; flex-direction: column; gap: 10px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
.sec h3 { margin: 0; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.explain { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--text); }
.explain.m { color: var(--muted); font-size: 12px; }
.errline { margin: 0; font-size: 12px; color: var(--danger-text); }
.swrow { display: flex; align-items: center; gap: 9px; font-size: 13px; cursor: pointer; }
.finding {
  display: grid; grid-template-columns: 16px 1fr; gap: 8px; padding: 8px 10px; border-radius: 8px;
  font-size: 12.5px; line-height: 1.45; border: 1px solid var(--border); background: var(--bg-soft);
}
.finding.e { border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); background: var(--tint-danger); }
.finding.e > .ic { margin-top: 2px; color: var(--danger-text); }
.finding.w > .ic { margin-top: 2px; color: var(--warning); }
.finding .where { margin-bottom: 2px; font-size: 11.5px; color: var(--muted); }
.members { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.member {
  width: 100%; display: flex; align-items: center; gap: 8px; padding: 6px 10px; text-align: left;
  border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); color: var(--text); font-size: 12.5px;
}
.member .nm { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.member code { font-size: 11.5px; color: var(--muted); }
.member .k { margin-left: auto; font-size: 11.5px; color: var(--muted); }
@media (hover: hover) and (pointer: fine) { .member:hover { border-color: var(--accent); } }
.line { margin: 0; padding: 7px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border-soft); font-size: 12px; white-space: pre-wrap; word-break: break-word; }
.hyper { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border-soft); background: var(--bg-soft); }
.hh { display: flex; align-items: center; gap: 6px; font-size: 12.5px; }
.hh .link { margin-left: auto; padding: 0; border: 0; background: none; color: var(--muted); font-size: 11.5px; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
@media (hover: hover) and (pointer: fine) { .hh .link:hover { color: var(--accent); } }
/* below 1400 px the panel keeps 420 px: the chart goes under the numbers */
@media (max-width: 1399px) { .cols { grid-template-columns: minmax(0, 1fr); } }
</style>
