<script setup lang="ts">
// The fields a node owns by its kind (docs/specs/bayes-graph-editor.md, «The inspector»), under its
// label, name and box: a parameter's prior (the distribution, its arguments, the bounds); a computed
// value's formula; a linear predictor's intercept and terms; a likelihood's observed data, its
// distribution and arguments; a latent state's first value, its scale and its form; correlated
// effects' components and correlation; a data node's type, its whole numbers and its
// standardization. Each change is a function of the document handed to the page (`change`): a
// choice is a step of the history at once, typing becomes one when it stops.
import { computed, reactive, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CircleAlert, Plus, X } from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import BayesSlotField from '~/components/bayes/BayesSlotField.vue'
import { bayesArgSymbol, bayesConstraintKey } from '~/composables/bayesEditorText'
import { BAYES_CATALOG, bayesDistribution, bayesLimit } from '~/composables/bayesCatalog'
import {
  BAYES_DATA_TYPES, BAYES_INT_SUPPORTS, bayesAddComponent, bayesAddTerm, bayesRemoveComponent, bayesRemoveTerm, bayesSetDist,
  bayesSetEffectsText, bayesSetStandardize, bayesSetStateForm, bayesSetSupport, bayesSetDataType, bayesWriteSlot,
  type BayesDistPart, type BayesDoc,
} from '~/composables/bayesGraphEdits'
import { bayesIsPriorPath, bayesNameChoices, type BayesNameChoice, type BayesSlotView } from '~/composables/bayesInspector'
import type { BayesGraphFinding, BayesModel } from '~/composables/bayesGraph'
import type { BayesTemplateNode, BayesTemplateSlot } from '~/composables/bayesTemplates'

const props = defineProps<{
  doc: BayesDoc
  node: BayesTemplateNode
  model: BayesModel | null
  findings: BayesGraphFinding[]
  /** The node's slots as the inspector shows them, by path. */
  views: Map<string, BayesSlotView>
  say: (f: BayesGraphFinding) => string
  typeWords: (plate: string) => string
  labelOf: (id: string) => string
}>()
const emit = defineEmits<{
  (e: 'change', fn: (d: BayesDoc) => unknown, step: boolean): void
  (e: 'escape'): void
}>()
const { t, te, locale } = useI18n()

const NONE = '-'
const id = computed(() => props.node.id)
const fid = (path: string) => `bf-${id.value}-${path.replace(/[^a-zA-Z0-9]/g, '-')}`
const change = (fn: (d: BayesDoc) => unknown, step = true) => emit('change', fn, step)
const write = (path: string, text: string) => change(d => bayesWriteSlot(d, id.value, path, { expr: text }), false)
const num = (v: number): string => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 6 }).format(v)

// ---------------------------------------------------------------- names at the caret

// a node never reads itself (that is a cycle): its own names are left out of its lists
const ownIds = (c: BayesNameChoice) => !c.node || (c.node !== id.value && !c.node.startsWith(`${id.value}.`))
const namesAny = computed(() => bayesNameChoices(props.doc, false).filter(ownIds))
const namesPrior = computed(() => bayesNameChoices(props.doc, true).filter(ownIds))
const namesFor = (path: string) => (bayesIsPriorPath(path) ? namesPrior.value : namesAny.value)
function roleWords (role: string): string {
  if (role === 'component') return t('bayes.editor.names.component')
  if (role.startsWith('data_')) return t(`bayes.editor.palette.item.${role.slice(5)}.name`)
  return te(`bayes.editor.kind.${role}`) ? t(`bayes.editor.kind.${role}`) : role
}
function nameHint (c: BayesNameChoice): string {
  if (c.kind === 'function') return te(`bayes.fn.${c.text}`) ? t(`bayes.fn.${c.text}`) : ''
  return `${roleWords(c.role ?? '')} · ${props.typeWords(c.plate ?? '')}`
}

// ---------------------------------------------------------------- distributions

// the catalogue's priors (the LKJ only inside correlated effects) and its likelihoods
const byUse = (use: 'prior' | 'likelihood') => BAYES_CATALOG.distributions.filter(d => d.uses.includes(use))
const priorOptions = computed(() => byUse('prior').filter(d => d.id !== 'lkj_corr_cholesky').map(d => ({ value: d.id, label: t(`bayes.dist.${d.id}.name`), hint: t(`bayes.dist.${d.id}.what`) })))
const likelihoodOptions = computed(() => byUse('likelihood').map(d => ({ value: d.id, label: t(`bayes.dist.${d.id}.name`), hint: t(`bayes.dist.${d.id}.what`) })))
function distOf (part: BayesDistPart): string {
  const n = props.node
  return part === 'likelihood' ? String(n.dist ?? '') : (part === 'prior' ? n.prior : n.init)?.dist ?? ''
}
/** The arguments of a distribution, in the catalogue's order, with their words. */
function argRows (part: BayesDistPart) {
  const dist = distOf(part)
  const d = bayesDistribution(dist)
  const prefix = part === 'likelihood' ? 'args' : part
  return (d?.params ?? []).map((p) => {
    const path = `${prefix}.${p.name}`
    const key = bayesConstraintKey(p.constraint)
    return {
      path,
      view: props.views.get(path),
      symbol: bayesArgSymbol(p.name),
      words: te(`bayes.dist.${dist}.param.${p.name}`) ? t(`bayes.dist.${dist}.param.${p.name}`) : p.name,
      constraint: key ? t(key) : '',
    }
  }).filter(r => r.view)
}
const setDist = (part: BayesDistPart, v: unknown) => change(d => bayesSetDist(d, id.value, part, String(v)))

// ---------------------------------------------------------------- a parameter's bounds

const bounds = computed(() => (props.node.bounds ?? {}) as { lower?: number | null, upper?: number | null })
const param = computed(() => props.model?.params.get(id.value))
const badBound = reactive({ lower: false, upper: false })
watch(id, () => { badBound.lower = false; badBound.upper = false })
function onBound (which: 'lower' | 'upper', e: Event) {
  const raw = (e.target as HTMLInputElement).value.trim().replace(',', '.')
  const v = Number(raw)
  badBound[which] = raw !== '' && !Number.isFinite(v)
  if (badBound[which]) return
  change((d) => {
    const n = d.nodes.find(x => x.id === id.value)
    if (!n) return
    const b = { ...((n.bounds as Record<string, number> | undefined) ?? {}) }
    if (raw === '') delete b[which]
    else b[which] = v
    if (Object.keys(b).length) n.bounds = b
    else delete n.bounds
  }, false)
}
const boundFindings = computed(() => props.findings.filter(f => f.node === id.value && f.code === 'graph.bounds'))
const cutRange = computed(() => [bounds.value.lower != null ? `≥ ${num(bounds.value.lower)}` : '', bounds.value.upper != null ? `≤ ${num(bounds.value.upper)}` : ''].filter(Boolean).join(', '))

// ---------------------------------------------------------------- data nodes in selects

const dataNodes = computed(() => props.doc.nodes.filter(n => n.kind === 'data'))
const dataOption = (n: BayesTemplateNode) => ({ value: n.id, label: String(n.name || n.label), hint: n.name ? n.label : undefined })
const observedOptions = computed(() => [{ value: NONE, label: t('bayes.editor.fields.chooseData') }, ...dataNodes.value.map(dataOption)])
// a term multiplies a number, a whole number or a category: a group index or a time is never one
function termOptions (current: string) {
  const fit = dataNodes.value.filter(n => n.type === 'real' || n.type === 'int' || n.type === 'category' || n.id === current)
  return [{ value: NONE, label: t('bayes.editor.fields.chooseData') }, ...fit.map(dataOption)]
}
const slotError = (path: string): string => {
  const v = props.views.get(path)
  return v?.error ? props.say(v.error) : ''
}

// ---------------------------------------------------------------- a linear predictor

const terms = computed(() => ((props.node.terms ?? []) as Array<{ data: string, coef: BayesTemplateSlot }>).map((x, i) => {
  const data = props.doc.nodes.find(n => n.id === x.data)
  const pair = props.model?.pairs.some(p => p.node === id.value && p.term === i)
  return { i, data: x.data, view: props.views.get(`terms[${i}].coef`), dataPath: `terms[${i}].data`, std: !!pair && !!data?.standardize, dataName: String(data?.name ?? data?.label ?? '') }
}))
function setIntercept (text: string) {
  // an empty intercept is none: the sum starts with its first term
  change((d) => {
    const n = d.nodes.find(x => x.id === id.value)
    if (!n) return
    n.intercept = text.trim() === '' ? null : { expr: text }
  }, false)
}
const interceptView = computed(() => props.views.get('intercept'))

// ---------------------------------------------------------------- correlated effects

const maxComponents = bayesLimit('max_effect_components')
const components = computed(() => (props.node.components ?? []).map((c, i) => ({
  ...c, n: i + 1, mean: props.views.get(`${c.id}.mean`), scale: props.views.get(`${c.id}.scale`),
})))
const etaView = computed(() => props.views.get('correlation.eta'))
const effectsText = (part: string, field: 'label' | 'name', e: Event) => change(d => bayesSetEffectsText(d, id.value, part, field, (e.target as HTMLInputElement).value), false)
// a component's or the correlation's name and label are checked as a node's are, under `<node>.<part>`
const partErrors = (part: string) => props.findings.filter(f => f.node === `${id.value}.${part}` && (f.code.startsWith('graph.name') || f.code === 'graph.label'))

// ---------------------------------------------------------------- a data node

const typeOptions = computed(() => BAYES_DATA_TYPES.map(x => ({ value: x, label: t(`bayes.editor.palette.item.${x}.name`), hint: t(`bayes.editor.palette.item.${x}.what`) })))
const supportOptions = computed(() => BAYES_INT_SUPPORTS.map(x => ({ value: x, label: t(`bayes.editor.dataType.int_${x}`) })))
const observedBy = computed(() => props.doc.nodes.filter(n => n.kind === 'likelihood' && n.observed === id.value))
const stateNonCentred = computed(() => (props.node.form ?? 'non_centred') !== 'centred')
</script>

<template>
  <!-- a parameter: its prior and its bounds -->
  <section v-if="node.kind === 'parameter'" class="sec">
    <h3>{{ t('bayes.editor.inspector.prior') }}</h3>
    <div class="fl">
      <label :for="fid('dist')">{{ t('bayes.editor.inspector.distribution') }}</label>
      <Select :id="fid('dist')" :model-value="distOf('prior')" :options="priorOptions" @update:model-value="(v: unknown) => setDist('prior', v)" />
    </div>
    <BayesSlotField
      v-for="a in argRows('prior')"
      :id="fid(a.path)"
      :key="a.path"
      :view="a.view!"
      :symbol="a.symbol"
      :words="a.words"
      :constraint="a.constraint"
      :names="namesFor(a.path)"
      :say="say"
      :type-words="typeWords"
      :name-hint="nameHint"
      @input="(v: string) => write(a.path, v)"
      @escape="emit('escape')"
    />
    <div class="row2">
      <div v-for="which in (['lower', 'upper'] as const)" :key="which" class="fl">
        <label :for="fid(which)">{{ t(`bayes.editor.bounds.${which}`) }}</label>
        <input
          :id="fid(which)"
          class="nokey"
          type="text"
          inputmode="decimal"
          :value="bounds[which] ?? ''"
          :placeholder="t('bayes.editor.bounds.none')"
          :aria-invalid="badBound[which] ? 'true' : 'false'"
          :aria-describedby="badBound[which] ? `${fid(which)}-err` : undefined"
          @input="onBound(which, $event)"
          @keydown.esc="emit('escape')"
        >
        <p v-if="badBound[which]" :id="`${fid(which)}-err`" class="errline">{{ t('bayes.editor.bounds.notNumber') }}</p>
      </div>
    </div>
    <p v-for="(f, i) in boundFindings" :key="i" class="errline"><CircleAlert :size="13" aria-hidden="true" />{{ say(f) }}</p>
    <p v-if="param?.truncated" class="explain">{{ t('bayes.editor.bounds.cut', { range: cutRange }) }}</p>
  </section>

  <!-- a computed value -->
  <section v-else-if="node.kind === 'transformed' && views.get('value')" class="sec">
    <h3>{{ t('bayes.editor.fields.formula') }}</h3>
    <BayesSlotField
      :id="fid('value')"
      :view="views.get('value')!"
      :words="t('bayes.editor.slot.value')"
      :names="namesFor('value')"
      :say="say"
      :type-words="typeWords"
      :name-hint="nameHint"
      :placeholder="t('bayes.editor.fields.formulaPlaceholder')"
      @input="(v: string) => write('value', v)"
      @escape="emit('escape')"
    />
    <p class="explain m">{{ t('bayes.editor.fields.formulaWhat') }}</p>
  </section>

  <!-- a linear predictor: the intercept and the terms -->
  <section v-else-if="node.kind === 'linear_predictor'" class="sec">
    <h3>{{ t('bayes.editor.fields.sum') }}</h3>
    <BayesSlotField
      v-if="interceptView"
      :id="fid('intercept')"
      :view="interceptView"
      :words="t('bayes.editor.slot.intercept')"
      :names="namesFor('intercept')"
      :say="say"
      :type-words="typeWords"
      :name-hint="nameHint"
      :placeholder="t('bayes.editor.fields.noIntercept')"
      @input="setIntercept"
      @escape="emit('escape')"
    />
    <div v-else class="fl">
      <label :for="fid('intercept')">{{ t('bayes.editor.slot.intercept') }}</label>
      <button :id="fid('intercept')" type="button" class="mini add" @click="change(d => bayesWriteSlot(d, id, 'intercept', { expr: '' }))">
        <Plus :size="13" aria-hidden="true" />{{ t('bayes.editor.fields.addIntercept') }}
      </button>
    </div>
    <div v-for="x in terms" :key="x.i" class="term">
      <div class="termhead">
        <span class="tn">{{ t('bayes.editor.fields.term', { n: x.i + 1 }) }}</span>
        <button type="button" class="mini icon" :aria-label="t('bayes.editor.fields.removeTerm', { n: x.i + 1 })" :title="t('bayes.editor.fields.removeTerm', { n: x.i + 1 })" @click="change(d => bayesRemoveTerm(d, id, x.i))">
          <X :size="13" aria-hidden="true" />
        </button>
      </div>
      <div class="row2">
        <div class="fl">
          <label :for="fid(x.dataPath)">{{ t('bayes.editor.fields.termData') }}</label>
          <Select :id="fid(x.dataPath)" :model-value="x.data || NONE" :options="termOptions(x.data)" @update:model-value="(v: unknown) => change(d => bayesWriteSlot(d, id, x.dataPath, v === NONE ? '' : String(v)))" />
        </div>
        <BayesSlotField
          v-if="x.view"
          :id="fid(`terms[${x.i}].coef`)"
          :view="x.view"
          :words="t('bayes.editor.fields.coef')"
          :names="namesFor('coef')"
          :say="say"
          :type-words="typeWords"
          :name-hint="nameHint"
          @input="(v: string) => write(`terms[${x.i}].coef`, v)"
          @escape="emit('escape')"
        />
      </div>
      <p v-if="slotError(x.dataPath)" class="errline"><CircleAlert :size="13" aria-hidden="true" />{{ slotError(x.dataPath) }}</p>
      <p v-if="x.std" class="explain m">{{ t('bayes.editor.fields.bothScales', { name: x.dataName }) }}</p>
    </div>
    <button type="button" class="mini add" @click="change(d => bayesAddTerm(d, id))">
      <Plus :size="13" aria-hidden="true" />{{ t('bayes.editor.fields.addTerm') }}
    </button>
  </section>

  <!-- a likelihood: the observed data, the distribution and its arguments -->
  <template v-else-if="node.kind === 'likelihood'">
    <section class="sec">
      <h3>{{ t('bayes.editor.slot.observed') }}</h3>
      <div class="fl">
        <label :for="fid('observed')">{{ t('bayes.editor.fields.observedWhat') }}</label>
        <Select :id="fid('observed')" :model-value="node.observed ? String(node.observed) : NONE" :options="observedOptions" @update:model-value="(v: unknown) => change(d => bayesWriteSlot(d, id, 'observed', v === NONE ? '' : String(v)))" />
        <p v-if="slotError('observed')" class="errline"><CircleAlert :size="13" aria-hidden="true" />{{ slotError('observed') }}</p>
      </div>
    </section>
    <section class="sec">
      <h3>{{ t('bayes.editor.inspector.distribution') }}</h3>
      <div class="fl">
        <label :for="fid('dist')">{{ t('bayes.editor.fields.likelihoodDist') }}</label>
        <Select :id="fid('dist')" :model-value="distOf('likelihood')" :options="likelihoodOptions" @update:model-value="(v: unknown) => setDist('likelihood', v)" />
      </div>
      <BayesSlotField
        v-for="a in argRows('likelihood')"
        :id="fid(a.path)"
        :key="a.path"
        :view="a.view!"
        :symbol="a.symbol"
        :words="a.words"
        :constraint="a.constraint"
        :names="namesFor(a.path)"
        :say="say"
        :type-words="typeWords"
        :name-hint="nameHint"
        @input="(v: string) => write(a.path, v)"
        @escape="emit('escape')"
      />
    </section>
  </template>

  <!-- a latent state: its first value, its steps and its form -->
  <template v-else-if="node.kind === 'state'">
    <section class="sec">
      <h3>{{ t('bayes.editor.fields.process') }}</h3>
      <p class="explain">{{ t('bayes.editor.fields.randomWalk') }}</p>
    </section>
    <section class="sec">
      <h3>{{ t('bayes.editor.fields.firstValue') }}</h3>
      <div class="fl">
        <label :for="fid('dist')">{{ t('bayes.editor.inspector.distribution') }}</label>
        <Select :id="fid('dist')" :model-value="distOf('init')" :options="priorOptions" @update:model-value="(v: unknown) => setDist('init', v)" />
      </div>
      <BayesSlotField
        v-for="a in argRows('init')"
        :id="fid(a.path)"
        :key="a.path"
        :view="a.view!"
        :symbol="a.symbol"
        :words="a.words"
        :constraint="a.constraint"
        :names="namesFor(a.path)"
        :say="say"
        :type-words="typeWords"
        :name-hint="nameHint"
        @input="(v: string) => write(a.path, v)"
        @escape="emit('escape')"
      />
    </section>
    <section class="sec">
      <h3>{{ t('bayes.editor.fields.steps') }}</h3>
      <BayesSlotField
        v-if="views.get('scale')"
        :id="fid('scale')"
        :view="views.get('scale')!"
        symbol="σ"
        :words="t('bayes.editor.slot.scale')"
        :constraint="t('bayes.editor.constraint.positive')"
        :names="namesFor('scale')"
        :say="say"
        :type-words="typeWords"
        :name-hint="nameHint"
        @input="(v: string) => write('scale', v)"
        @escape="emit('escape')"
      />
      <label class="swrow">
        <input type="checkbox" role="switch" :checked="stateNonCentred" @change="change(d => bayesSetStateForm(d, id, ($event.target as HTMLInputElement).checked))">
        {{ t('bayes.editor.plate.nonCentred') }}
      </label>
      <p class="explain m">{{ t(stateNonCentred ? 'bayes.editor.fields.stateNonCentred' : 'bayes.editor.fields.stateCentred') }}</p>
    </section>
  </template>

  <!-- correlated effects: the components and their correlation -->
  <template v-else-if="node.kind === 'correlated_effects'">
    <section class="sec">
      <h3>{{ t('bayes.editor.fields.components', { n: components.length }) }}</h3>
      <div v-for="c in components" :key="c.id" class="term">
        <div class="termhead">
          <span class="tn">{{ t('bayes.editor.fields.component', { n: c.n }) }}</span>
          <code v-if="c.name" class="cn">{{ c.name }}</code>
          <button
            v-if="components.length > 2"
            type="button"
            class="mini icon"
            :aria-label="t('bayes.editor.fields.removeComponent', { name: c.name || c.n })"
            :title="t('bayes.editor.fields.removeComponent', { name: c.name || c.n })"
            @click="change(d => bayesRemoveComponent(d, id, c.id))"
          >
            <X :size="13" aria-hidden="true" />
          </button>
        </div>
        <div class="row2">
          <div class="fl">
            <label :for="fid(`${c.id}.label`)">{{ t('bayes.editor.inspector.label') }}</label>
            <input :id="fid(`${c.id}.label`)" class="nokey" type="text" :value="c.label" @input="effectsText(c.id, 'label', $event)" @keydown.esc="emit('escape')">
          </div>
          <div class="fl">
            <label :for="fid(`${c.id}.name`)">{{ t('bayes.editor.inspector.name') }}</label>
            <input :id="fid(`${c.id}.name`)" class="nokey mono" type="text" spellcheck="false" autocapitalize="off" autocomplete="off" :value="c.name" @input="effectsText(c.id, 'name', $event)" @keydown.esc="emit('escape')">
          </div>
        </div>
        <p v-for="(f, i) in partErrors(c.id)" :key="i" class="errline"><CircleAlert :size="13" aria-hidden="true" />{{ say(f) }}</p>
        <div class="row2">
          <BayesSlotField
            v-if="c.mean"
            :id="fid(`${c.id}.mean`)"
            :view="c.mean"
            symbol="μ"
            :words="t('bayes.editor.fields.mean')"
            :names="namesFor(`${c.id}.mean`)"
            :say="say"
            :type-words="typeWords"
            :name-hint="nameHint"
            @input="(v: string) => write(`${c.id}.mean`, v)"
            @escape="emit('escape')"
          />
          <BayesSlotField
            v-if="c.scale"
            :id="fid(`${c.id}.scale`)"
            :view="c.scale"
            symbol="σ"
            :words="t('bayes.editor.fields.scale')"
            :names="namesFor(`${c.id}.scale`)"
            :say="say"
            :type-words="typeWords"
            :name-hint="nameHint"
            @input="(v: string) => write(`${c.id}.scale`, v)"
            @escape="emit('escape')"
          />
        </div>
      </div>
      <button
        type="button"
        class="mini add"
        :disabled="components.length >= maxComponents"
        :title="components.length >= maxComponents ? t('bayes.editor.fields.maxComponents', { max: maxComponents }) : undefined"
        @click="change(d => bayesAddComponent(d, id, t('bayes.editor.fields.newComponent', { n: components.length + 1 }), maxComponents))"
      >
        <Plus :size="13" aria-hidden="true" />{{ t('bayes.editor.fields.addComponent') }}
      </button>
    </section>
    <section v-if="node.correlation" class="sec">
      <h3>{{ t('bayes.editor.fields.correlation') }}</h3>
      <div class="row2">
        <div class="fl">
          <label :for="fid('correlation.label')">{{ t('bayes.editor.inspector.label') }}</label>
          <input :id="fid('correlation.label')" class="nokey" type="text" :value="node.correlation.label" @input="effectsText('correlation', 'label', $event)" @keydown.esc="emit('escape')">
        </div>
        <div class="fl">
          <label :for="fid('correlation.name')">{{ t('bayes.editor.inspector.name') }}</label>
          <input :id="fid('correlation.name')" class="nokey mono" type="text" spellcheck="false" autocapitalize="off" autocomplete="off" :value="node.correlation.name" @input="effectsText('correlation', 'name', $event)" @keydown.esc="emit('escape')">
        </div>
      </div>
      <p v-for="(f, i) in partErrors('correlation')" :key="i" class="errline"><CircleAlert :size="13" aria-hidden="true" />{{ say(f) }}</p>
      <BayesSlotField
        v-if="etaView"
        :id="fid('correlation.eta')"
        :view="etaView"
        symbol="η"
        :words="t('bayes.dist.lkj_corr_cholesky.param.eta')"
        :constraint="t('bayes.editor.fields.etaConstraint')"
        :names="[]"
        :say="say"
        :type-words="typeWords"
        :name-hint="nameHint"
        @input="(v: string) => write('correlation.eta', v)"
        @escape="emit('escape')"
      />
      <p class="explain m">{{ t('bayes.dist.lkj_corr_cholesky.what') }}</p>
    </section>
  </template>

  <!-- a data node: its type, its whole numbers, its standardization -->
  <section v-else-if="node.kind === 'data'" class="sec">
    <h3>{{ t('bayes.editor.fields.column') }}</h3>
    <div class="row2">
      <div class="fl">
        <label :for="fid('type')">{{ t('bayes.editor.fields.type') }}</label>
        <Select :id="fid('type')" :model-value="String(node.type ?? 'real')" :options="typeOptions" @update:model-value="(v: unknown) => change(d => bayesSetDataType(d, id, String(v)))" />
      </div>
      <div v-if="node.type === 'int'" class="fl">
        <label :for="fid('support')">{{ t('bayes.editor.fields.support') }}</label>
        <Select :id="fid('support')" :model-value="String(node.support ?? 'any')" :options="supportOptions" @update:model-value="(v: unknown) => change(d => bayesSetSupport(d, id, String(v)))" />
      </div>
    </div>
    <template v-if="node.type === 'real'">
      <label class="swrow">
        <input type="checkbox" role="switch" :checked="!!node.standardize" @change="change(d => bayesSetStandardize(d, id, ($event.target as HTMLInputElement).checked))">
        {{ t('bayes.editor.fields.standardize') }}
      </label>
      <p class="explain m">{{ t(observedBy.length ? 'bayes.editor.fields.standardizeObserved' : 'bayes.editor.fields.standardizeWhat') }}</p>
    </template>
    <p v-if="observedBy.length" class="explain">{{ t('bayes.editor.fields.observedBy', { label: observedBy.map(n => n.label).join(', ') }) }}</p>
    <p class="explain m">{{ t('bayes.editor.fields.bound') }}</p>
  </section>
</template>

<style scoped>
.sec { display: flex; flex-direction: column; gap: 10px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
.sec h3 { margin: 0; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.fl { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.fl > label { font-size: 12px; font-weight: 500; color: var(--muted); }
.fl :deep(.sel-trigger), .fl :deep(button[role="combobox"]) { width: 100%; }
.row2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; align-items: start; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.explain { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--text); }
.explain.m { color: var(--muted); font-size: 12px; }
.errline { display: grid; grid-template-columns: 15px 1fr; gap: 6px; margin: 0; font-size: 12px; line-height: 1.45; color: var(--danger-text); }
.errline :deep(svg) { margin-top: 2px; }
.swrow { display: flex; align-items: center; gap: 9px; font-size: 13px; cursor: pointer; }
/* a term or a component: a group of fields under a rule, never a card in a card */
.term { display: flex; flex-direction: column; gap: 8px; padding: 2px 0 10px 10px; border-left: 1px solid var(--border); }
.termhead { display: flex; align-items: center; gap: 8px; min-height: 24px; }
.termhead .tn { font-size: 12px; font-weight: 600; color: var(--text); }
.termhead .cn { font-size: 11.5px; color: var(--muted); }
.termhead .icon { margin-left: auto; }
.mini.icon { display: inline-grid; place-items: center; width: 24px; padding: 0; }
.mini.add { align-self: flex-start; display: inline-flex; align-items: center; gap: 5px; }
</style>
