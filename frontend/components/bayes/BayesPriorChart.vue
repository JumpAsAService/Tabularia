<script setup lang="ts">
// The chart of a prior beside its numbers (docs/specs/bayes-graph-editor.md, «The chart beside the
// numbers»): it asks the preview (bayesPriorPreview.ts, in its worker, 150 ms after the last change,
// only the latest answer drawn) for the target's marginal and draws the curve (closed form) or the
// histogram (ancestral draws: with hyperpriors, the true marginal), the quantiles 5, 50 and 95 % as
// three marks, and the preview's sentences under it. While an argument has an error the last valid
// form stays, dimmed, under «last valid form: fix μ to update it».
//
// Drawn as an SVG of a few rectangles and lines in the theme's tokens (the approved mockup's
// drawing): it follows the five themes with no colour read at runtime.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { BarChart3, Dices, Pencil } from 'lucide-vue-next'
import { useBayesPriorPreview } from '~/composables/bayesPriorPreview'
import type { BayesPriorAnswer } from '~/composables/bayesPriorCore'
import type { BayesPriorPlan } from '~/composables/bayesGraph'

const props = defineProps<{
  plan: BayesPriorPlan | null
  target: string
  /** How the sentences call the quantity («Mean of the school»). */
  name: string
  /** Its name in code, for the table of quantiles. */
  code?: string
  /** The header's words. */
  title?: string
  /** The argument of this prior that has an error («μ»), for «fix μ to update it». */
  staleArg?: string | null
  /** The node's own findings stop the chart (an error anywhere above it). */
  blocked?: boolean
  /** The small form of the hyperprior's card: the curve and one sentence. */
  compact?: boolean
  /** Labels of the plan's nodes, for the left-out draws' sentence. */
  labelOf?: (id: string) => string
}>()
const { t, locale } = useI18n()
const preview = useBayesPriorPreview()
const seed = ref(1)

const planKey = computed(() => (props.plan ? JSON.stringify(props.plan) : ''))
watch([planKey, () => props.target, seed], () => {
  if (props.plan) preview.request(props.plan, props.target, seed.value)
}, { immediate: true })

const answer = computed(() => preview.state.value.answer)
const drawable = (a: BayesPriorAnswer | null | undefined): a is BayesPriorAnswer => !!a && a.kind !== 'none' && !!a.x && !!a.y
// the last form drawn for this target: kept, dimmed, while the numbers have an error
const lastValid = ref<BayesPriorAnswer | null>(null)
watch(answer, (a) => { if (drawable(a) && a.target === props.target) lastValid.value = a })
watch(() => props.target, () => { lastValid.value = null; seed.value = 1 })

const stale = computed(() => !!props.staleArg || !!props.blocked || (answer.value?.kind === 'none' && answer.value.none === 'errors'))
const shown = computed<BayesPriorAnswer | null>(() => {
  if (stale.value) return lastValid.value
  return drawable(answer.value) && answer.value.target === props.target ? answer.value : (preview.state.value.pending ? lastValid.value : null)
})
const none = computed(() => (!stale.value && answer.value?.kind === 'none' && answer.value.target === props.target ? answer.value : null))

// ---------------------------------------------------------------- numbers

function fmt (v: number): string {
  const a = Math.abs(v)
  const digits = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 3
  return new Intl.NumberFormat(locale.value, { maximumFractionDigits: digits }).format(v)
}
const pct = (p: number): string => new Intl.NumberFormat(locale.value, { style: 'percent', maximumFractionDigits: 1 }).format(p)
const count = (n: number): string => new Intl.NumberFormat(locale.value).format(n)

// ---------------------------------------------------------------- the drawing

const W = 280
const H = computed(() => (props.compact ? 74 : 118))
const TOP = 14
const BOTTOM = 16

const geometry = computed(() => {
  const a = shown.value
  if (!a?.x || !a.y) return null
  const x = a.x
  const y = a.y
  const x0 = x[0]!
  const x1 = x[x.length - 1]!
  const span = x1 - x0 || 1
  const sx = (v: number): number => ((v - x0) / span) * W
  const plotH = H.value - TOP - BOTTOM
  let max = 0
  for (const v of y) if (Number.isFinite(v) && v > max) max = v
  const sy = (v: number): number => TOP + plotH - (max > 0 ? Math.min(v / max, 1) : 0) * plotH
  const floor = TOP + plotH
  if (a.kind === 'histogram') {
    const bars = []
    for (let i = 0; i < y.length; i++) {
      const left = sx(x[i]!)
      const right = sx(x[i + 1]!)
      const top = sy(y[i]!)
      if (floor - top < 0.5) continue
      bars.push({ x: left + 0.4, w: Math.max(right - left - 0.8, 0.6), y: top, h: floor - top })
    }
    return { kind: 'histogram' as const, bars, sx, floor, x0, x1 }
  }
  let d = ''
  let open = false
  for (let i = 0; i < x.length; i++) {
    const v = y[i]!
    if (!Number.isFinite(v)) { open = false; continue }
    d += `${open ? 'L' : 'M'}${sx(x[i]!).toFixed(2)} ${sy(v).toFixed(2)} `
    open = true
  }
  const area = d ? `${d}L${W} ${floor} L0 ${floor} Z` : ''
  return { kind: 'curve' as const, d, area, sx, floor, x0, x1 }
})

const marks = computed(() => {
  const a = shown.value
  const g = geometry.value
  if (!a?.quantiles || !g) return []
  const q = a.quantiles
  return [
    { at: g.sx(q.q5), label: '5 %' },
    { at: g.sx(q.q50), label: '50 %' },
    { at: g.sx(q.q95), label: '95 %' },
  ].map(m => ({ ...m, at: Math.min(Math.max(m.at, 0), W) }))
})
// the marks' words, spread apart where the marks crowd (a skewed prior puts 5 % and 50 % side by side):
// each at least 11 % of the width from the one before, all within the drawing
const labelAt = computed(() => {
  const GAP = 11
  const at = marks.value.map(m => (m.at / W) * 100)
  for (let i = 1; i < at.length; i++) at[i] = Math.max(at[i]!, at[i - 1]! + GAP)
  const over = (at[at.length - 1] ?? 0) - 96
  if (over > 0) for (let i = at.length - 1; i >= 0; i--) at[i] = Math.min(at[i]!, 96 - (at.length - 1 - i) * GAP)
  for (let i = 0; i < at.length; i++) at[i] = Math.max(at[i]!, 4 + i * GAP)
  return at
})
const cuts = computed(() => {
  const a = shown.value
  if (!a) return { below: false, above: false }
  if (a.kind === 'histogram') return { below: (a.cut?.below ?? 0) > 0, above: (a.cut?.above ?? 0) > 0 }
  return { below: (a.cutMass?.below ?? 0) > 0, above: (a.cutMass?.above ?? 0) > 0 }
})

// ---------------------------------------------------------------- the words

const quantileWords = computed(() => {
  const q = shown.value?.quantiles
  return q ? t('bayes.prior.quantiles', { q5: fmt(q.q5), q50: fmt(q.q50), q95: fmt(q.q95) }) : ''
})
const summary = computed(() => {
  const q = shown.value?.quantiles
  return q ? t('bayes.prior.summary', { name: props.name, low: fmt(q.q5), high: fmt(q.q95), median: fmt(q.q50) }) : ''
})
const facts = computed(() => {
  const a = shown.value
  if (!a || props.compact) return []
  const out: string[] = []
  if (a.kind === 'curve') out.push(t('bayes.prior.exact'))
  else if (a.mixture) out.push(t('bayes.prior.mixture', { n: count(a.draws ?? 0), name: props.name }))
  if (a.truncated) out.push(t('bayes.prior.truncated'))
  if (a.kind === 'histogram' && a.cut && (a.cut.below || a.cut.above)) out.push(t('bayes.prior.cut.histogram', { below: count(a.cut.below), above: count(a.cut.above) }))
  if (a.kind === 'curve' && a.cutMass && (a.cutMass.below || a.cutMass.above)) out.push(t('bayes.prior.cut.curve', { below: pct(a.cutMass.below), above: pct(a.cutMass.above) }))
  if (a.leftOut && a.leftOut.count > 0) {
    out.push(t(`bayes.prior.leftOut.${a.leftOut.reason}`, { count: count(a.leftOut.count), total: count(a.draws ?? 0), node: props.labelOf?.(a.leftOut.detail.node) ?? a.leftOut.detail.node }))
  }
  return out
})
const noneText = computed(() => {
  const a = none.value
  if (!a?.none) return ''
  if (a.none === 'constant') return t('bayes.prior.none.constant', { value: fmt(a.quantiles?.q50 ?? 0) })
  return t(`bayes.prior.none.${a.none}`)
})
const headSub = computed(() => {
  const a = shown.value
  if (!a) return ''
  return a.kind === 'curve' ? t('bayes.editor.chart.exactCurve') : t('bayes.editor.chart.draws', { n: count(a.draws ?? 0) })
})
</script>

<template>
  <div class="chart" :class="{ stale: stale && !!shown, compact }">
    <h4 v-if="!compact">
      <BarChart3 :size="13" aria-hidden="true" />
      <span>{{ title }}</span>
      <span v-if="headSub" class="sub">{{ headSub }}</span>
    </h4>
    <template v-if="shown && geometry">
      <div class="plotwrap">
        <svg class="plot" :viewBox="`0 0 ${W} ${H}`" role="img" :aria-label="`${name}: ${quantileWords}`" preserveAspectRatio="none">
          <template v-if="geometry.kind === 'histogram'">
            <rect v-for="(b, i) in geometry.bars" :key="i" class="bar" :x="b.x" :y="b.y" :width="b.w" :height="b.h" />
          </template>
          <template v-else>
            <path class="area" :d="geometry.area" />
            <path class="curve" :d="geometry.d" vector-effect="non-scaling-stroke" />
          </template>
          <line class="floor" x1="0" :x2="W" :y1="geometry.floor" :y2="geometry.floor" vector-effect="non-scaling-stroke" />
          <line v-if="cuts.below" class="cut" x1="0.5" x2="0.5" :y1="TOP" :y2="geometry.floor" vector-effect="non-scaling-stroke" />
          <line v-if="cuts.above" class="cut" :x1="W - 0.5" :x2="W - 0.5" :y1="TOP" :y2="geometry.floor" vector-effect="non-scaling-stroke" />
          <line v-for="m in marks" :key="m.label" class="qm" :x1="m.at" :x2="m.at" :y1="TOP - 2" :y2="geometry.floor" vector-effect="non-scaling-stroke" />
        </svg>
        <!-- the marks' and the axis' words in HTML, so that the stretched drawing never stretches them -->
        <div class="qlabels" aria-hidden="true">
          <span v-for="(m, i) in marks" :key="m.label" :style="{ left: `${labelAt[i]}%` }">{{ m.label }}</span>
        </div>
        <div class="axis" aria-hidden="true">
          <span>{{ fmt(geometry.x0) }}</span>
          <span>{{ fmt((geometry.x0 + geometry.x1) / 2) }}</span>
          <span>{{ fmt(geometry.x1) }}</span>
        </div>
        <p v-if="stale" class="stalebar"><Pencil :size="12" class="ic" aria-hidden="true" />{{ staleArg ? t('bayes.editor.chart.stale', { arg: staleArg }) : t('bayes.editor.chart.staleOther') }}</p>
      </div>
      <table v-if="!compact && shown.quantiles" class="qt">
        <caption class="sr-only">{{ t('bayes.editor.chart.tableCaption', { name }) }}</caption>
        <thead><tr><th scope="col"><span class="sr-only">{{ t('bayes.editor.chart.quantity') }}</span></th><th scope="col">5 %</th><th scope="col">50 %</th><th scope="col">95 %</th></tr></thead>
        <tbody><tr><th scope="row"><code>{{ code || target }}</code></th><td>{{ fmt(shown.quantiles.q5) }}</td><td>{{ fmt(shown.quantiles.q50) }}</td><td>{{ fmt(shown.quantiles.q95) }}</td></tr></tbody>
      </table>
      <p class="say">{{ summary }}</p>
      <p v-for="(f, i) in facts" :key="i" class="say m">{{ f }}</p>
      <div v-if="!compact && shown.kind === 'histogram'" class="drawrow">
        <button type="button" class="mini" :disabled="stale" @click="seed++"><Dices :size="13" aria-hidden="true" />{{ t('bayes.prior.drawAgain') }}</button>
        <span class="muted">{{ t('bayes.prior.seed', { seed }) }}</span>
      </div>
    </template>
    <p v-else-if="noneText" class="say m">{{ noneText }}</p>
    <p v-else-if="stale" class="say m">{{ t('bayes.editor.chart.neverValid') }}</p>
    <p v-else-if="preview.state.value.failed" class="say m">{{ t('bayes.prior.failed') }}</p>
    <div v-else class="sk-plot" :style="{ height: `${H}px` }" aria-busy="true"><span class="sr-only">{{ t('bayes.prior.drawing') }}</span></div>
  </div>
</template>

<style scoped>
.chart { position: relative; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
h4 { margin: 0; display: flex; align-items: flex-start; gap: 6px; line-height: 1.35; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
h4 :deep(svg) { flex: none; margin-top: 1px; }
h4 .sub { margin-left: auto; flex: none; text-transform: none; letter-spacing: 0; font-weight: 400; font-size: 11.5px; white-space: nowrap; }
.plotwrap { position: relative; padding-top: 14px; }
.plot { display: block; width: 100%; height: 118px; overflow: visible; }
.compact .plot { height: 74px; }
.bar { fill: var(--accent); }
.curve { fill: none; stroke: var(--accent); stroke-width: 1.8; }
.area { fill: color-mix(in srgb, var(--accent) 16%, transparent); }
.floor { stroke: var(--border); stroke-width: 1; }
.cut { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 2 3; }
.qm { stroke: var(--text); stroke-width: 1; opacity: 0.75; }
.qlabels { position: absolute; left: 0; right: 0; top: 0; height: 14px; font-size: 10.5px; color: var(--muted); }
.qlabels span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.axis { display: flex; justify-content: space-between; margin-top: 3px; font-size: 10.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
/* the last valid form while an argument has an error: there, dimmed, and said */
.stale .plot, .stale .qlabels, .stale .axis { opacity: 0.38; filter: saturate(0.5); }
.stalebar {
  position: absolute; left: 50%; top: 44%; transform: translate(-50%, -50%); margin: 0;
  display: flex; align-items: center; gap: 6px; padding: 3px 10px; white-space: nowrap;
  font-size: 11.5px; color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 999px; box-shadow: var(--shadow-1);
}
.stalebar .ic { color: var(--muted); }
.qt { width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
.qt th { font-weight: 500; color: var(--muted); font-size: 11px; text-align: right; padding: 2px 6px; }
.qt td { text-align: right; padding: 2px 6px; border-top: 1px solid var(--border-soft); }
.qt th:first-child { text-align: left; padding-left: 0; }
.qt tbody th { border-top: 1px solid var(--border-soft); font-weight: 400; }
.qt tbody th code { font-size: 11.5px; color: var(--muted); }
.stale .qt { opacity: 0.55; }
.say { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--text); }
.say.m { font-size: 12px; color: var(--muted); }
.stale .say { color: var(--muted); }
.compact .say { font-size: 12px; color: var(--muted); }
.drawrow { display: flex; align-items: center; gap: 8px; }
.drawrow .muted { font-size: 11.5px; color: var(--muted); }
.sk-plot { border-radius: 6px; background: var(--panel-2); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
</style>
