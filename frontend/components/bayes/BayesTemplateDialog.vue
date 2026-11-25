<script setup lang="ts">
// What opening a template asks (docs/specs/bayes-templates.md, «Opening a template»; the mockup of the
// template's dialog): the outcome's typical value and spread, written in the priors in the open, and
// for the AR template its order p (1 to bayesTemplateMaxOrder). Every field is optional; the table
// under them shows the priors those numbers give, as they will be written, before anything opens.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { BookOpen, Eye, Info, LayoutTemplate, Minus, Plus } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import { bayesDistribution } from '~/composables/bayesCatalog'
import { bayesFormatNumber } from '~/composables/bayesExpr'
import { bayesBoundsText } from '~/composables/bayesEditorText'
import { BAYES_TEMPLATES, bayesOpenTemplate, bayesTemplateMaxOrder, type BayesOpenOptions, type BayesTemplate, type BayesTemplateNode } from '~/composables/bayesTemplates'

const props = defineProps<{ template: BayesTemplate, open: boolean }>()
const emit = defineEmits<{ (e: 'open', options: BayesOpenOptions): void, (e: 'cancel'): void }>()
const { t, te, locale } = useI18n()
const texts = (key: string): string | undefined => (te(key) ? t(key) : undefined)

const center = ref('')
const spread = ref('')
const order = ref(props.template.order?.default ?? 2)
const maxOrder = computed(() => bayesTemplateMaxOrder(props.template))
watch(() => props.template.id, () => { center.value = ''; spread.value = ''; order.value = props.template.order?.default ?? 2 })

const parse = (s: string): number | null => {
  const v = s.trim().replace(/\s/g, '').replace(',', '.')
  if (v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : Number.NaN
}
const centerValue = computed(() => parse(center.value))
const spreadValue = computed(() => parse(spread.value))
const centerBad = computed(() => Number.isNaN(centerValue.value))
const spreadBad = computed(() => Number.isNaN(spreadValue.value) || (spreadValue.value !== null && spreadValue.value <= 0))
const orderBad = computed(() => !Number.isInteger(order.value) || order.value < 1 || order.value > maxOrder.value)
const asks = computed(() => ({ scale: props.template.scale === 'outcome', order: !!props.template.order }))

const options = computed<BayesOpenOptions>(() => ({
  ...(asks.value.scale && !centerBad.value && centerValue.value !== null ? { center: centerValue.value } : {}),
  ...(asks.value.scale && !spreadBad.value && spreadValue.value !== null ? { spread: spreadValue.value } : {}),
  ...(asks.value.order && !orderBad.value ? { order: order.value } : {}),
}))
const invalid = computed(() => (asks.value.scale && (centerBad.value || spreadBad.value)) || (asks.value.order && orderBad.value))

// the graph these numbers give, read for the table (a number out of range opens nothing)
const graph = computed(() => {
  try { return bayesOpenTemplate(props.template.id, options.value, texts) } catch { return null }
})

const isLag = (id: string): boolean => {
  const o = props.template.order
  return !!o && id.startsWith(o.id_prefix) && /^\d+$/.test(id.slice(o.id_prefix.length))
}
type Part = { text: string, mark: boolean }
function priorParts (n: BayesTemplateNode, holder: 'prior' | 'init'): Part[] {
  const p = n[holder] as BayesTemplateNode['prior']
  if (!p) return []
  const order = bayesDistribution(p.dist)?.params.map(x => x.name) ?? Object.keys(p.args)
  const out: Part[] = [{ text: `${p.dist}(`, mark: false }]
  order.forEach((arg, i) => {
    if (i) out.push({ text: ', ', mark: false })
    const text = p.args[arg]?.expr ?? p.args[arg]?.ref ?? ''
    const scaled = props.template.scaled.find(s => s.node === n.id && s.slot === `${holder}.${arg}`)
    const given = scaled ? (scaled.as === 'center' ? options.value.center : options.value.spread) : undefined
    const word = given != null ? bayesFormatNumber(given) : ''
    const at = word ? text.lastIndexOf(word) : -1
    // the person's own number, tinted where it was written
    if (at >= 0 && (scaled!.as === 'center' ? given !== 0 : given !== 1)) {
      if (at > 0) out.push({ text: text.slice(0, at), mark: false })
      out.push({ text: word, mark: true })
      if (at + word.length < text.length) out.push({ text: text.slice(at + word.length), mark: false })
    } else out.push({ text, mark: false })
  })
  out.push({ text: ')', mark: false })
  const b = bayesBoundsText(n.bounds as { lower?: number, upper?: number } | undefined, v => new Intl.NumberFormat(locale.value).format(v))
  if (b) out.push({ text: `, ${b}`, mark: false })
  return out
}

const rows = computed(() => {
  const g = graph.value
  if (!g) return []
  const out: Array<{ key: string, label: string, names: string, parts: Part[] }> = []
  const lags = g.nodes.filter(n => n.kind === 'parameter' && isLag(n.id))
  for (const n of g.nodes) {
    if (n.kind === 'parameter' && isLag(n.id)) {
      if (n.id !== lags[0]?.id) continue
      const names = lags.length > 3 ? `${lags[0]!.name}, … ${lags[lags.length - 1]!.name}` : lags.map(x => x.name).join(', ')
      out.push({ key: 'lags', label: t('bayes.editor.dialog.lags', { n: lags.length }), names, parts: priorParts(n, 'prior') })
    } else if (n.kind === 'parameter') {
      out.push({ key: n.id, label: n.label, names: String(n.name ?? ''), parts: priorParts(n, 'prior') })
    } else if (n.kind === 'state' && n.init) {
      out.push({ key: n.id, label: t('bayes.editor.dialog.stateInit', { label: n.label }), names: String(n.name ?? ''), parts: priorParts(n, 'init') })
    }
  }
  return out
})
const likelihood = computed(() => {
  const lik = graph.value?.nodes.find(n => n.kind === 'likelihood')
  if (!lik || !asks.value.order) return ''
  const order = bayesDistribution(String(lik.dist))?.params.map(x => x.name) ?? []
  const observed = graph.value?.nodes.find(n => n.id === lik.observed)?.name ?? ''
  return `${observed} ~ ${lik.dist}(${order.map(a => lik.args?.[a]?.expr ?? '').join(', ')})`
})
const sources = computed(() => {
  const keys = [...new Set(Object.values(props.template.sources))].filter(k => k !== 'spec')
  return keys.map(k => BAYES_TEMPLATES.sources[k]).filter(Boolean) as Array<{ title: string, url: string }>
})
const why = computed(() => {
  if (asks.value.scale && asks.value.order) return t('bayes.editor.dialog.whyScaleOrder')
  if (asks.value.scale) return t('bayes.editor.dialog.whyScale')
  return t('bayes.editor.dialog.whyNone')
})

function step (d: number) {
  order.value = Math.min(Math.max(1, (Number.isInteger(order.value) ? order.value : 1) + d), maxOrder.value)
}
function submit () {
  if (invalid.value) return
  emit('open', options.value)
}
</script>

<template>
  <Modal :open="open" :title="t(`bayes.templates.${template.id}.title`)" :width="540" @close="emit('cancel')">
    <template #head><LayoutTemplate :size="16" class="headic" aria-hidden="true" /></template>
    <form class="tpl" novalidate @submit.prevent="submit">
      <p class="lead">{{ t(`bayes.templates.${template.id}.what`) }}</p>
      <p class="why">{{ why }}</p>
      <div v-if="asks.scale || asks.order" class="fields" :class="{ three: asks.scale && asks.order }">
        <template v-if="asks.scale">
          <div class="fl">
            <label for="tpl-center">{{ t('bayes.editor.dialog.center') }}</label>
            <input id="tpl-center" v-model="center" type="text" inputmode="decimal" autocomplete="off" :aria-invalid="centerBad ? 'true' : 'false'" aria-describedby="tpl-center-hint">
            <span id="tpl-center-hint" class="hint" :class="{ bad: centerBad }">{{ centerBad ? t('bayes.editor.dialog.notNumber') : t('bayes.editor.dialog.centerHint') }}</span>
          </div>
          <div class="fl">
            <label for="tpl-spread">{{ t('bayes.editor.dialog.spread') }}</label>
            <input id="tpl-spread" v-model="spread" type="text" inputmode="decimal" autocomplete="off" :aria-invalid="spreadBad ? 'true' : 'false'" aria-describedby="tpl-spread-hint">
            <span id="tpl-spread-hint" class="hint" :class="{ bad: spreadBad }">{{ spreadBad ? t('bayes.editor.dialog.notPositive') : t('bayes.editor.dialog.spreadHint') }}</span>
          </div>
        </template>
        <div v-if="asks.order" class="fl">
          <label for="tpl-order">{{ t('bayes.editor.dialog.order') }}</label>
          <div class="numwrap">
            <input id="tpl-order" v-model.number="order" type="number" min="1" :max="maxOrder" step="1" :aria-invalid="orderBad ? 'true' : 'false'" aria-describedby="tpl-order-hint">
            <button type="button" :aria-label="t('bayes.editor.dialog.orderLess')" :title="t('bayes.editor.dialog.orderLess')" :disabled="order <= 1" @click="step(-1)"><Minus :size="13" /></button>
            <button type="button" :aria-label="t('bayes.editor.dialog.orderMore')" :title="t('bayes.editor.dialog.orderMore')" :disabled="order >= maxOrder" @click="step(1)"><Plus :size="13" /></button>
          </div>
          <span id="tpl-order-hint" class="hint" :class="{ bad: orderBad }">{{ t('bayes.editor.dialog.orderHint', { max: maxOrder }) }}</span>
        </div>
      </div>
      <div v-if="rows.length" class="pv">
        <h3><Eye :size="13" aria-hidden="true" />{{ asks.order && !orderBad ? t('bayes.editor.dialog.priorsP', { p: order }) : t('bayes.editor.dialog.priors') }}</h3>
        <table>
          <tbody>
            <tr v-for="r in rows" :key="r.key">
              <td class="n">{{ r.label }}<small>{{ r.names }}</small></td>
              <td class="p"><template v-for="(part, i) in r.parts" :key="i"><mark v-if="part.mark">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></td>
            </tr>
          </tbody>
        </table>
        <div v-if="likelihood" class="lik">{{ t('bayes.editor.dialog.likelihood') }}<code>{{ likelihood }}</code></div>
      </div>
      <p v-if="asks.order" class="src"><Info :size="13" aria-hidden="true" /><span>{{ t('bayes.editor.dialog.arFacts', { p: orderBad ? 'p' : order }) }}</span></p>
      <p v-if="sources.length" class="src">
        <BookOpen :size="13" aria-hidden="true" />
        <span>{{ t('bayes.editor.dialog.sources') }} <template v-for="(s, i) in sources" :key="s.url"><template v-if="i"> · </template><a :href="s.url" target="_blank" rel="noopener noreferrer">{{ s.title }}</a></template>. {{ t('bayes.editor.dialog.changeLater') }}</span>
      </p>
      <button type="submit" class="sr-only" tabindex="-1" aria-hidden="true" />
    </form>
    <template #footer>
      <span class="note">{{ t('bayes.editor.dialog.local') }}</span>
      <button type="button" @click="emit('cancel')">{{ t('bayes.editor.dialog.cancel') }}</button>
      <button type="button" class="primary" :disabled="invalid" @click="submit"><LayoutTemplate :size="14" aria-hidden="true" />{{ t('bayes.editor.dialog.open') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.headic { order: -1; color: var(--muted); }
.tpl { display: flex; flex-direction: column; gap: 14px; }
.lead { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--muted); }
.why { margin: 0; font-size: 12.5px; line-height: 1.5; }
.fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.fields.three { grid-template-columns: 1fr 1fr 0.85fr; }
.fl { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.fl > label { font-size: 12px; font-weight: 500; color: var(--muted); }
.fl input { font-variant-numeric: tabular-nums; }
.hint { font-size: 12px; line-height: 1.4; color: var(--muted); }
.hint.bad { color: var(--danger-text); }
.numwrap { display: flex; align-items: stretch; }
.numwrap input { border-radius: 8px 0 0 8px; min-width: 0; -moz-appearance: textfield; }
.numwrap input::-webkit-inner-spin-button, .numwrap input::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
.numwrap button { border-radius: 0; border-left: 0; padding: 6px 8px; border-color: var(--control-border); }
.numwrap button:last-child { border-radius: 0 8px 8px 0; }
.pv { border: 1px solid var(--border-soft); border-radius: var(--radius); background: var(--bg-soft); }
.pv h3 { margin: 0; display: flex; align-items: center; gap: 6px; padding: 9px 12px 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.pv table { width: 100%; border-collapse: collapse; font-size: 12px; }
.pv td { padding: 6px 12px; border-top: 1px solid var(--border-soft); vertical-align: top; }
.pv td.n { width: 48%; }
.pv td.n small { display: block; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
.pv td.p { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; white-space: nowrap; }
.pv td.p mark { background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--text); border-radius: 3px; padding: 0 2px; }
.pv .lik { padding: 8px 12px 10px; border-top: 1px solid var(--border-soft); font-size: 11.5px; line-height: 1.5; color: var(--muted); }
.pv .lik code { display: block; margin-top: 3px; color: var(--text); font-size: 11.5px; white-space: normal; word-break: break-word; }
.src { margin: 0; display: grid; grid-template-columns: 14px 1fr; gap: 6px; font-size: 11.5px; line-height: 1.45; color: var(--muted); }
.src :deep(svg) { margin-top: 2px; }
.src a { color: var(--muted); text-decoration: underline; text-underline-offset: 2px; }
@media (hover: hover) and (pointer: fine) { .src a:hover { color: var(--accent); } }
.note { margin-right: auto; font-size: 11.5px; color: var(--muted); }
</style>
