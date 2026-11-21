// The experiments' nodes in the editor (lot E7 of docs/specs/ab-testing.md), without a browser: the form of the A/B test and of
// its planning, the expected split of the variants (the control `level_numbers`), a combination that is not allowed shown grey
// with its reason, «Register the model as» left out for a node that writes no model, the columns the node hands on before it
// has run (the variant keeps the input's own type: the `=variant` marker), and the words in the five languages.
// Run: npm test
import { test, after } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createI18n } from 'vue-i18n'

register('./alias-hooks.mjs', import.meta.url)
const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const OUT = join(FRONTEND, 'tests', `.rendered-experiments-${process.pid}`)
mkdirSync(OUT, { recursive: true })
after(() => rmSync(OUT, { recursive: true, force: true }))

Reflect.set(globalThis, 'useApiClient', () => ({ apiFetch: async () => { throw new Error('no gateway here') } }))
const m = await import('../composables/modelCatalog.ts')
const icons = await import('../composables/modelIcons.ts')

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const LANGS = ['en', 'it', 'de', 'es', 'fr']
const MESSAGES = {}
const flat = (tree, prefix = '', out = {}) => {
  for (const [k, v] of Object.entries(tree)) {
    if (typeof v === 'string') out[prefix + k] = v
    else flat(v, `${prefix}${k}.`, out)
  }
  return out
}
const WORDS = {}
for (const lang of LANGS) {
  MESSAGES[lang] = { ...(await import(`../i18n/locales/${lang}.ts`)).default, ...(await import(`../i18n/locales/${lang}.gen.ts`)).default }
  WORDS[lang] = flat((await import(`../i18n/locales/${lang}.gen.ts`)).default)
}

const AB = m.modelOf('ab_test')
const PLAN = m.modelOf('ab_plan')
const COLUMNS = [
  { name: 'arm', dtype: 'String' }, { name: 'user', dtype: 'Int64' }, { name: 'converted', dtype: 'Boolean' },
  { name: 'revenue', dtype: 'Float64' }, { name: 'clicks', dtype: 'Int64' }, { name: 'views', dtype: 'Int64' },
  { name: 'entered', dtype: 'Date' }, { name: 'stamp', dtype: "Datetime(time_unit='us', time_zone=None)" }, { name: 'bucket', dtype: 'Int32' },
]

// ── the model and its fields ─────────────────────────────────────────────────

test('the A/B test: what it needs, its fields in the catalogue\'s order, the split drawn by its own control', () => {
  assert.equal(AB.card, false)
  assert.equal(PLAN.card, false)
  assert.equal(m.modelOf('linear').card, true)
  const by = Object.fromEntries(m.modelFields(AB).map((f) => [f.key, f]))
  assert.deepEqual(m.modelFields(AB).filter((f) => f.required).map((f) => f.key), ['variant', 'control', 'metric_type'])
  assert.deepEqual([by.variant.control, by.control.control, by.metric_type.control, by.input_shape.control, by.expected_shares.control, by.secondary_metrics.control],
    ['column', 'level', 'choice', 'choice', 'level_numbers', 'columns'])
  assert.equal(by.expected_shares.of, 'variant')
  assert.equal(by.expected_shares.above, 0)
  assert.equal(by.control.of, 'variant')
  assert.deepEqual(by.alternative.choices, ['two_sided', 'greater', 'less'])
  assert.deepEqual(by.correction.choices, ['holm', 'benjamini_hochberg', 'bonferroni', 'none'])
  const plan = Object.fromEntries(m.modelFields(PLAN).map((f) => [f.key, f]))
  assert.deepEqual([plan.solve_for.control, plan.arms.control, plan.days.control, plan.units_per_variant.control, plan.traffic_share.control], ['choice', 'integer', 'integer', 'integer', 'number'])
  assert.equal(plan.arms.max, m.MODEL_CATALOG.limits.max_variants)
})

test('a new A/B test starts with the defaults of the spec; the keys that do not apply are not kept', () => {
  const d = m.modelDefaults('ab_test')
  assert.equal(d.srm_alpha, 0.001)
  assert.equal(d.min_lift, 0.02)
  assert.equal(d.power, 0.8)
  assert.equal(d.loss_threshold, 0.001)
  assert.equal(d.correction, 'holm')
  assert.ok(!('sequential_tau' in d) && !('covariate_missing' in d) && !('expected_shares' in d))
  // aggregated rows: no unit, no metric column, no covariate, no cap, no secondary metrics; the sums instead
  const base = { variant: 'arm', control: 'A', metric_type: 'continuous' }
  const kept = (p) => Object.keys(m.modelParams(AB, p))
  assert.deepEqual(kept({ ...base, input_shape: 'aggregated', unit: 'user', metric: 'revenue', covariate: 'revenue', cap_percentile: 0.99, secondary_metrics: ['revenue'], units_count: 'n', metric_sum: 's', metric_sum_squares: 'q' }),
    ['variant', 'control', 'metric_type', 'input_shape', 'units_count', 'metric_sum', 'metric_sum_squares'])
  // a binary metric has no sum of squares and no cap, a ratio has no metric column but a numerator and a denominator
  assert.ok(!kept({ ...base, metric_type: 'binary', cap_percentile: 0.99 }).includes('cap_percentile'))
  assert.deepEqual(kept({ ...base, metric_type: 'ratio', metric: 'x', numerator: 'clicks', denominator: 'views' }), ['variant', 'control', 'metric_type', 'numerator', 'denominator'])
  // the priors with a binary metric and the Bayesian reading only; tau with the sequential reading only
  assert.ok(kept({ ...base, metric_type: 'binary', prior_alpha: 2 }).includes('prior_alpha'))
  assert.ok(!kept({ ...base, prior_alpha: 2 }).includes('prior_alpha'))
  assert.ok(!kept({ ...base, metric_type: 'binary', bayes: false, prior_alpha: 2 }).includes('prior_alpha'))
  assert.ok(kept({ ...base, sequential: true, sequential_tau: 0.1 }).includes('sequential_tau'))
  assert.ok(!kept({ ...base, sequential_tau: 0.1 }).includes('sequential_tau'))
})

test('what the A/B test still lacks is said by the catalogue\'s rules, with the reasons', () => {
  assert.deepEqual(m.modelUnset(AB, {}), ['variant', 'control', 'metric_type'])
  const base = { variant: 'arm', control: 'A' }
  assert.deepEqual(m.modelUnset(AB, { ...base, metric_type: 'binary' }), ['metric'])
  assert.deepEqual(m.modelUnset(AB, { ...base, metric_type: 'ratio' }), ['numerator', 'denominator'])
  assert.deepEqual(m.modelUnset(AB, { ...base, metric_type: 'continuous', input_shape: 'aggregated' }), ['units_count', 'metric_sum', 'metric_sum_squares'])
  assert.deepEqual(m.modelUnset(AB, { ...base, metric_type: 'binary', input_shape: 'aggregated' }), ['units_count', 'metric_sum'])
  assert.equal(m.modelRequiredReason(AB, 'metric_sum_squares', { ...base, metric_type: 'continuous', input_shape: 'aggregated' })?.reason, 'sum_squares_needed')
  assert.equal(m.modelRequiredReason(AB, 'metric_sum_squares', { ...base, metric_type: 'binary', input_shape: 'aggregated' }), null)
  // the planning: what each question needs
  assert.deepEqual(m.modelUnset(PLAN, { metric_type: 'binary', metric: 'x', solve_for: 'mde_for_units' }), ['units_per_variant'])
  assert.deepEqual(m.modelUnset(PLAN, { metric_type: 'binary', metric: 'x', solve_for: 'mde_for_days' }), ['days', 'date'])
  assert.deepEqual(m.modelUnset(PLAN, { metric_type: 'binary', metric: 'x', solve_for: 'sample_size' }), [])
  assert.ok(!('min_lift' in m.modelParams(PLAN, { metric_type: 'binary', metric: 'x', solve_for: 'mde_for_units', min_lift: 0.05 })))
  assert.ok(!('traffic_share' in m.modelParams(PLAN, { metric_type: 'binary', metric: 'x', traffic_share: 0.5 })))
  assert.ok('traffic_share' in m.modelParams(PLAN, { metric_type: 'binary', metric: 'x', date: 'entered', traffic_share: 0.5 }))
})

// ── the columns each key may take ────────────────────────────────────────────

test('the variant, the metric and the date columns take the kinds of the catalogue, as the engine reads a type', () => {
  const by = Object.fromEntries(m.modelFields(AB).map((f) => [f.key, f]))
  const fits = (key) => COLUMNS.filter((c) => m.modelColumnFits(by[key], c.dtype)).map((c) => c.name)
  assert.deepEqual(fits('variant'), ['arm', 'user', 'converted', 'clicks', 'views', 'entered', 'bucket'])
  assert.deepEqual(fits('metric'), ['user', 'converted', 'revenue', 'clicks', 'views', 'bucket'])
  assert.deepEqual(fits('date'), ['entered', 'stamp'])
  assert.deepEqual(fits('covariate'), ['user', 'revenue', 'clicks', 'views', 'bucket'])
  assert.deepEqual(fits('secondary_metrics'), ['user', 'converted', 'revenue', 'clicks', 'views', 'bucket'])
  // the kinds, as the engine's column_kind names them
  const kinds = { Int64: 'integer', UInt8: 'integer', Float64: 'number', 'Decimal(10, 2)': 'number', String: 'text', Categorical: 'text', Boolean: 'boolean', Date: 'date',
    "Datetime(time_unit='ms', time_zone='UTC')": 'datetime', Time: 'other', Duration: 'other', 'List(Int64)': 'other' }
  for (const [dtype, kind] of Object.entries(kinds)) assert.equal(m.modelColumnKind(dtype), kind, dtype)
  // the older time and order columns keep their meaning
  const time = { types: ['date', 'datetime', 'integer'] }
  assert.deepEqual(['Date', "Datetime(time_unit='us', time_zone=None)", 'Int64', 'Float64', 'String'].filter((d) => m.modelColumnFits(time, d)), ['Date', "Datetime(time_unit='us', time_zone=None)", 'Int64'])
})

test('the variant column\'s key is not offered as the metric or the unit: the model refuses a column in two places', () => {
  const params = { variant: 'arm', unit: 'user', metric: 'converted', secondary_metrics: ['revenue'] }
  assert.deepEqual([...m.modelTaken(AB, params, 'covariate')].sort(), ['arm', 'converted', 'revenue', 'user'])
  assert.deepEqual(m.modelOverlaps(AB, { ...params, covariate: 'converted' }), [{ column: 'converted', keys: ['metric', 'covariate'] }])
  assert.deepEqual(m.modelOverlaps(AB, { ...params, secondary_metrics: ['converted'] }), [{ column: 'converted', keys: ['metric', 'secondary_metrics'] }])
})

// ── the expected split ───────────────────────────────────────────────────────

test('the expected split is kept as numbers by variant, and goes when the variant column changes', () => {
  const base = { variant: 'arm', control: 'A', metric_type: 'binary', metric: 'converted' }
  assert.deepEqual(m.modelParams(AB, { ...base, expected_shares: { B: '1', A: '1', C: 2 } }).expected_shares, { B: 1, A: 1, C: 2 })
  // a box left empty, or not a number: not kept; none left: not given
  assert.deepEqual(m.modelParams(AB, { ...base, expected_shares: { A: 1, B: '', C: 'x' } }).expected_shares, { A: 1 })
  assert.ok(!('expected_shares' in m.modelParams(AB, { ...base, expected_shares: {} })))
  assert.ok(!('expected_shares' in m.modelParams(AB, { ...base, expected_shares: [1, 1] })))
  assert.ok(!('expected_shares' in m.modelParams(AB, { ...base, expected_shares: 0.5 })))
  // a decimal comma is no number in a box of the gateway's kind: «0,5» is read as a half, «1,000» is none
  assert.deepEqual(m.modelParams(AB, { ...base, expected_shares: { A: '0,5' } }).expected_shares, { A: 0.5 })
  assert.ok(!('expected_shares' in m.modelParams(AB, { ...base, expected_shares: { A: '1,000' } })))
  // the same once more: modelParams twice is modelParams once
  const once = m.modelParams(AB, { ...base, expected_shares: { B: '1', A: 1 } })
  assert.deepEqual(m.modelParams(AB, once), once)
  // another variant column: the shares of the old one do not stay, nor does the control
  const after = m.modelSet(AB, { ...base, expected_shares: { A: 1, B: 3 } }, 'variant', 'bucket')
  assert.ok(!('expected_shares' in after) && !('control' in after), JSON.stringify(after))
  assert.deepEqual(m.modelSet(AB, { ...base, expected_shares: { A: 1 } }, 'metric', 'revenue').expected_shares, { A: 1 })
  // a weight is above 0 (the form says so, as the model would)
  const field = m.modelFields(AB).find((f) => f.key === 'expected_shares')
  assert.equal(m.modelNumberProblem(field, 0), 'above')
  assert.equal(m.modelNumberProblem(field, -2), 'above')
  assert.equal(m.modelNumberProblem(field, 0.001), '')
})

test('the split is read as shares of its sum, and what is missing is named', () => {
  assert.deepEqual(m.modelLevelNumbersMissing({ A: 1, C: 2 }, ['A', 'B', 'C']), ['B'])
  assert.deepEqual(m.modelLevelNumbersMissing(undefined, ['A', 'B']), ['A', 'B'])
  assert.deepEqual(m.modelLevelNumbersMissing({ A: 1, B: 1 }, ['A', 'B']), [])
  assert.deepEqual(m.modelLevelShares({ A: 1, B: 1, C: 2 }, ['A', 'B', 'C']), [0.25, 0.25, 0.5])
  assert.deepEqual(m.modelLevelShares({ A: 9, B: 1 }, ['A', 'B']), [0.9, 0.1])
  assert.equal(m.modelLevelShares({ A: 1, B: 0 }, ['A', 'B']), null)
  assert.equal(m.modelLevelShares({ A: 1 }, ['A', 'B']), null)
  assert.equal(m.modelLevelShares({}, []), null)
})

// ── a combination that is not allowed ────────────────────────────────────────

test('a ratio with summed rows is grey with its reason, whichever of the two is chosen first', () => {
  const ratio = { variant: 'arm', control: 'A', metric_type: 'ratio' }
  const summed = { variant: 'arm', control: 'A', input_shape: 'aggregated' }
  // the rule is the catalogue's: it forbids the shape while the metric is a ratio …
  assert.equal(m.modelForbidden(AB, 'input_shape', 'aggregated', ratio)?.reason, 'ratio_needs_units')
  assert.equal(m.modelForbiddenOption(AB, 'input_shape', 'aggregated', ratio)?.reason, 'ratio_needs_units')
  assert.equal(m.modelForbiddenOption(AB, 'input_shape', 'units', ratio), null)
  // … and the metric is greyed too once the summed rows are chosen
  assert.equal(m.modelForbidden(AB, 'metric_type', 'ratio', summed), null)
  assert.equal(m.modelForbiddenOption(AB, 'metric_type', 'ratio', summed)?.reason, 'ratio_needs_units')
  assert.equal(m.modelForbiddenOption(AB, 'metric_type', 'binary', summed), null)
  assert.equal(m.modelForbiddenOption(AB, 'metric_type', 'ratio', { ...summed, input_shape: 'units' }), null)
  assert.equal(m.modelForbiddenOption(PLAN, 'metric_type', 'ratio', { input_shape: 'aggregated' })?.reason, 'ratio_needs_units')
  // the always-valid p-value is two-sided: one-sided alternatives are grey with the sequential reading on
  assert.equal(m.modelForbiddenOption(AB, 'alternative', 'greater', { sequential: true })?.reason, 'sequential_two_sided')
  assert.equal(m.modelForbiddenOption(AB, 'alternative', 'less', { sequential: true })?.reason, 'sequential_two_sided')
  assert.equal(m.modelForbiddenOption(AB, 'alternative', 'two_sided', { sequential: true }), null)
  assert.equal(m.modelForbiddenOption(AB, 'alternative', 'greater', {}), null)
  // the other models are not touched: Firth is grey for the probit link, and the link is not grey for Firth
  const logistic = m.modelOf('logistic')
  assert.equal(m.modelForbiddenOption(logistic, 'estimation', 'firth', { link: 'probit' })?.reason, 'firth_needs_logit')
})

// ── what is drawn ────────────────────────────────────────────────────────────

async function component (path) {
  const source = readFileSync(join(FRONTEND, path), 'utf8')
  const { descriptor, errors } = parse(source, { filename: path })
  assert.deepEqual(errors, [])
  const content = compileScript(descriptor, { id: path, inlineTemplate: true }).content
    .replace("import { isSuperseded } from '~/composables/usePreviewSlots'", 'const isSuperseded = () => false')
  const file = join(OUT, `${path.replace(/[^\w]/g, '_')}.ts`)
  writeFileSync(file, content)
  return (await import(pathToFileURL(file).href)).default
}
const Select = defineComponent({
  name: 'Select',
  inheritAttrs: false,
  props: ['modelValue', 'options', 'placeholder', 'searchable'],
  setup: (props, { attrs }) => () => h('div', { class: 'select', 'data-value': String(props.modelValue ?? ''), ...attrs },
    (props.options ?? []).map((o) => h('span', { class: 'option', 'data-hint': o.hint }, o.label ?? String(o.value)))),
})
async function render (comp, props, locale = 'en') {
  const app = createSSRApp({ render: () => h(comp, props) })
  app.use(createI18n({ legacy: false, locale, messages: { [locale]: MESSAGES[locale] }, missingWarn: false, fallbackWarn: false }))
  app.component('Select', Select)
  const warnings = []
  app.config.warnHandler = (w) => warnings.push(w)
  app.config.errorHandler = (e) => { throw e }
  const html = await renderToString(app)
  assert.deepEqual(warnings, [])
  return html
}
const text = (html) => html.replace(/<!--.*?-->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').trim()
const selectsOf = (html) => [...html.matchAll(/<div class="select"[^>]*>(.*?)<\/div>/g)].map((x) => text(x[1]))
const node = (model, params = {}, more = {}) => ({ nodeId: 'ab-1', data: { model, params, timeoutSeconds: 900, retries: 0, ...more }, inputColumns: COLUMNS })
const radio = (html, key, value) => html.match(new RegExp(`<input type="radio" name="model-ab-1-${key}"[^>]*value="${value}"[^>]*>`))?.[0] ?? ''

test('a new A/B test: its form from the catalogue, in its words, what it lacks said once', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node('ab_test', m.modelDefaults('ab_test')))
  const page = text(html)
  for (const words of ['Compares the variants of an experiment', 'Variant column', 'Control', 'Type of metric', 'The rows are', 'Unit column',
    'A higher value is better', 'Alternative', 'Significance level', 'Correction across treatments', 'Covariate before the experiment (CUPED)',
    'Level of the split check', 'Expected split of the variants', 'All equal', 'Given for each variant', 'Smallest lift that matters', 'Power',
    'Bayesian reading', 'Expected loss accepted', 'Date column', 'Sequential reading', 'Secondary metrics', 'Output',
    'Still to set: Variant column, Control, Type of metric.']) {
    assert.ok(page.includes(words), `«${words}» is not on the page: ${page}`)
  }
  // only what applies to a units form with a metric not chosen yet: no sums, no scale of the mixture
  for (const gone of ['Units in the row', 'Sum of the metric', 'Scale of the sequential mixture', 'Numerator', 'Metric ']) assert.ok(!page.includes(gone), gone)
  // the metric's own box once the type is chosen
  const binary = text(await render(Form, node('ab_test', { variant: 'arm', control: 'A', metric_type: 'binary' })))
  assert.ok(binary.includes('Metric') && binary.includes('Still to set: Metric.') && binary.includes('Beta prior: α') && binary.includes('Beta prior: β') && binary.includes('Cap at the percentile') === false)
  assert.ok(text(await render(Form, node('ab_test', { variant: 'arm', control: 'A', metric_type: 'continuous' }))).includes('Cap at the percentile'))
  // the variant column: text, whole number, true/false or date; no number with decimals, no date and time
  assert.equal(selectsOf(html)[0], 'arm user converted clicks views entered bucket')
  // the control: a value of that column, written or chosen; waits for the column
  assert.match(html, /id="model-ab-1-control"[^>]*disabled/)
  // the type of metric and the shape: buttons
  for (const v of ['binary', 'continuous', 'ratio']) assert.ok(radio(html, 'metric_type', v), v)
  for (const v of ['units', 'aggregated']) assert.ok(radio(html, 'input_shape', v), v)
  assert.match(radio(html, 'input_shape', 'units'), /checked/)
  assert.match(radio(html, 'correction', 'holm') || html, /checked|holm/)
  // the equal split is on, no number box for a variant
  assert.match(html, /<input type="radio" name="model-ab-1-expected_shares"[^>]*checked/)
  assert.ok(!html.includes('model-ab-1-expected_shares-share-0'))
  // the experiment writes no model: nothing to register
  const withMlflow = await render(Form, node('ab_test', {}, { mlflow: { connectionId: 3, experiment: 'checkout' } }))
  const mp = text(withMlflow)
  assert.ok(mp.includes('Experiment') && mp.includes('Continue without MLflow'))
  assert.ok(!mp.includes('Register the model as'), mp)
  assert.ok(mp.includes('This node writes no model'), mp)
  // a model that writes one still asks for the name
  assert.ok(text(await render(Form, node('linear', {}, { mlflow: { connectionId: 3, experiment: 'x' } }))).includes('Register the model as'))
})

test('a ratio and summed rows are grey with their reason on the form, one against the other', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const REASON = 'A ratio metric needs one row per unit'
  // a ratio: the summed rows are off, with the sentence
  let html = await render(Form, node('ab_test', { variant: 'arm', control: 'A', metric_type: 'ratio' }))
  assert.match(radio(html, 'input_shape', 'aggregated'), /disabled/)
  assert.ok(!/disabled/.test(radio(html, 'input_shape', 'units')))
  assert.ok(text(html).includes(REASON), 'the reason is on the page')
  // summed rows: the ratio is off, with the same sentence
  html = await render(Form, node('ab_test', { variant: 'arm', control: 'A', input_shape: 'aggregated' }))
  assert.match(radio(html, 'metric_type', 'ratio'), /disabled/)
  assert.ok(!/disabled/.test(radio(html, 'metric_type', 'binary')))
  assert.ok(text(html).includes(REASON))
  // the summed rows' keys, and not the units' ones
  const page = text(html)
  for (const words of ['Units in the row', 'Sum of the metric']) assert.ok(page.includes(words), words)
  for (const gone of ['Unit column', 'Covariate before the experiment', 'Secondary metrics', 'Cap at the percentile']) assert.ok(!page.includes(gone), gone)
  // both the planning's form
  html = await render(Form, node('ab_plan', { metric_type: 'ratio' }))
  assert.match(radio(html, 'input_shape', 'aggregated'), /disabled/)
  // with nothing wrong, nothing is grey
  html = await render(Form, node('ab_test', { variant: 'arm', control: 'A', metric_type: 'binary' }))
  assert.ok(!/disabled/.test(radio(html, 'input_shape', 'aggregated')) && !text(html).includes(REASON))
  // the one-sided alternatives with the sequential reading on
  html = await render(Form, node('ab_test', { variant: 'arm', control: 'A', metric_type: 'binary', sequential: true }))
  assert.match(radio(html, 'alternative', 'greater'), /disabled/)
  assert.match(radio(html, 'alternative', 'less'), /disabled/)
  assert.ok(!/disabled/.test(radio(html, 'alternative', 'two_sided')))
  assert.ok(text(html).includes('The always-valid p-value of this node is two-sided.'))
  assert.ok(text(html).includes('Scale of the sequential mixture'))
})

test('the expected split: a box for each value of the variant column once «given» is chosen, in the node\'s own words', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const params = { variant: 'arm', control: 'A', metric_type: 'binary', metric: 'converted', expected_shares: { A: 1, B: 1, C: 2 } }
  // the values of the column are read at the input: while they load, a placeholder (and the radio says «given»)
  const fetchDistinct = () => new Promise(() => {})
  const html = await render(Form, { ...node('ab_test', params), fetchDistinct })
  assert.match(html, /<input type="radio" name="model-ab-1-expected_shares"[^>]*checked[^>]*>\s*<span>Given for each variant/)
  assert.ok(html.includes('aria-busy="true"'))
  // not chosen yet: asked for
  const none = text(await render(Form, node('ab_test', { ...params, expected_shares: undefined, variant: undefined, control: undefined })))
  assert.ok(none.includes('All equal'))
  // the control of the form's template draws the control, and PredictForm is not asked about it
  assert.match(read('../components/ui/ModelForm.vue'), /f\.control === 'level_numbers'/)
  assert.doesNotMatch(read('../components/ui/PredictForm.vue'), /level_numbers/)
})

test('the form is usable with the keyboard alone: native controls, each named, none that needs F, Home, End or Menu', () => {
  const form = read('../components/ui/ModelForm.vue')
  const split = form.slice(form.indexOf("f.control === 'level_numbers'"), form.indexOf("f.control === 'boolean'"))
  assert.ok(split.includes('<fieldset') && split.includes('<legend'), 'a group with a legend')
  assert.match(split, /type="radio"/)
  assert.match(split, /type="number"/)
  assert.match(split, /:aria-label="\$t\('models\.form\.weightOf'/)
  assert.match(split, /<label :for="idOf\(f\.key, `share-\$\{i\}`\)"/)
  assert.match(split, /:aria-invalid=/)
  assert.match(split, /role="alert"/)
  // no handler of a key by name: Tab, Space, the arrows and Enter are the browser's own on a native control
  assert.doesNotMatch(split, /@keydown|@keyup|tabindex/)
  // the grey choices are disabled radios (out of the way of the arrows), the reason beside each in text, not in colour alone
  assert.match(form, /:disabled="!!forbidden\(f, c\) && chosen\(f\) !== c"/)
  assert.match(form, /<span v-if="forbidden\(f, c\)" class="why"/)
})

// ── the palette and the canvas ───────────────────────────────────────────────

test('the experiments\' nodes have their icons and their summary, and are in the palette under Experiments', () => {
  assert.notEqual(icons.modelIconOf('ab_test'), icons.modelIconOf('linear'))
  assert.notEqual(icons.modelIconOf('ab_plan'), icons.modelIconOf('ab_test'))
  const t = (k) => WORDS.en[k] ?? k
  const shown = m.MODEL_CATALOG.groups.filter((g) => g.id === 'experiments')
  const palette = m.modelPalette(shown, '', t)
  assert.deepEqual(palette.map((g) => g.group), ['experiments'])
  assert.deepEqual(palette[0].items.map((i) => i.kind), ['model:ab_test', 'model:ab_plan'])
  assert.ok(!m.MODEL_NOT_IN_THE_PALETTE_YET.has('ab_test') && !m.MODEL_NOT_IN_THE_PALETTE_YET.has('ab_plan'))
  // their names and hints are in the interface's words, as the palette shows them
  assert.equal(t('models.groups.experiments'), 'Experiments')
  assert.equal(t('models.ab_test.name'), 'A/B test')
  assert.equal(t('models.ab_plan.name'), 'A/B test planning')
  assert.equal(m.modelSummary('ab_test', { variant: 'arm', control: 'A', metric_type: 'binary', metric: 'converted' }), 'converted ~ arm')
  assert.equal(m.modelSummary('ab_test', { variant: 'arm', control: 'A', metric_type: 'ratio', numerator: 'clicks', denominator: 'views' }), 'clicks / views ~ arm')
  assert.equal(m.modelSummary('ab_test', { variant: 'arm', control: 'A', metric_type: 'continuous', input_shape: 'aggregated', units_count: 'n', metric_sum: 's' }), 'n, s ~ arm')
  assert.equal(m.modelSummary('ab_plan', { metric_type: 'binary', metric: 'converted' }), 'converted')
  assert.equal(m.modelSummary('ab_test', {}), '')
})

// ── what the node hands on before it has run ─────────────────────────────────

test('the variants output keeps the variant column\'s own type, as the engine says (the `=variant` marker)', () => {
  const names = (cols) => cols.map((c) => c.name)
  const results = m.modelOutputColumns(AB, { output: 'variants' }, []).columns
  assert.deepEqual(names(results), ['metric', 'role', 'variant', 'is_control', 'units', 'share', 'expected_share', 'mean', 'std_error', 'ci_low',
    'ci_high', 'difference', 'difference_ci_low', 'difference_ci_high', 'lift', 'lift_ci_low', 'lift_ci_high',
    'p_value', 'p_adjusted', 'decision', 'prob_beat_control', 'prob_best', 'expected_loss', 'sequential_p_value',
    'sequential_p_adjusted', 'sequential_lift_low', 'sequential_lift_high'])
  for (const own of ['Int64', 'Int32', 'Boolean', 'String', 'Date', "Datetime(time_unit='us', time_zone=None)"]) {
    const { columns, later } = m.modelOutputColumns(AB, { variant: 'arm', output: 'variants' }, [{ name: 'arm', dtype: own }, { name: 'x', dtype: 'Int64' }])
    const by = Object.fromEntries(columns.map((c) => [c.name, c.dtype]))
    assert.equal(by.variant, own, own)
    assert.deepEqual(later, [])
    assert.ok(!('x' in by) && !('arm' in by), 'no input column but the variant')
    assert.equal(by.units, 'Int64') // the Int32 rule is for `rows` and `@key` alone
  }
  const typeOf = (params, input) => Object.fromEntries(m.modelOutputColumns(AB, params, input).columns.map((c) => [c.name, c.dtype])).variant
  assert.equal(typeOf({ output: 'variants' }, [{ name: 'arm', dtype: 'Int64' }]), 'Unknown') // the column not chosen
  assert.equal(typeOf({ variant: 'gone', output: 'variants' }, [{ name: 'arm', dtype: 'Int64' }]), 'Unknown') // not in the input
  assert.equal(typeOf({ variant: 'arm', output: 'variants' }, undefined), 'Unknown')
  // the results table is the same whatever the variant column; the planning's grid has no input column at all
  assert.deepEqual(m.modelOutputColumns(AB, {}, [{ name: 'arm', dtype: 'Int64' }]).columns, Object.entries(m.MODEL_CATALOG.output_types.results).map(([name, dtype]) => ({ name, dtype })))
  const plan = m.modelOutputColumns(PLAN, { output: 'plan' }, [{ name: 'arm', dtype: 'Int64' }])
  assert.deepEqual(names(plan.columns), ['lift', 'mde_absolute', 'n_per_variant', 'n_control', 'n_total', 'days', 'weeks', 'n_per_variant_cuped', 'days_cuped', 'asked'])
  assert.deepEqual(plan.later, [])
  assert.equal(plan.columns.at(-1).dtype, 'Boolean')
  assert.ok(plan.columns.slice(0, -1).every((c) => c.dtype === 'Float64'))
  // the marker is the same as the engine's: «=variant» in the catalogue, «@key» for the others stays
  assert.equal(m.MODEL_CATALOG.output_types.variants.variant, '=variant')
  const backend = read('../../backend/app/engine/models/catalog.py')
  assert.match(backend, /elif kind\.startswith\("="\):/)
})

// ── the words ────────────────────────────────────────────────────────────────

test('every word the experiments\' form asks for is in the five languages, with the same holes', () => {
  const keys = [...m.modelWordKeys(m.MODEL_CATALOG)].filter((k) => /ab_|reasons\.(ratio|sequential|metric|numerator|denominator|units|sum_squares|days)/.test(k) || k.startsWith('models.params.') || k.startsWith('models.choice.'))
  assert.ok(keys.length > 60)
  const missing = []
  for (const lang of LANGS) for (const key of keys) if (!WORDS[lang][key]?.trim()) missing.push(`${lang} ${key}`)
  assert.deepEqual(missing, [])
  const own = ['models.form.equalSplit', 'models.form.givenSplit', 'models.form.weightOf', 'models.form.splitMissing', 'models.form.splitNotPositive', 'models.form.splitIs',
    'models.form.tooManyVariants', 'models.form.noKindColumns', 'models.mlflow.noModelToRegister', 'models.params.expected_shares', 'models.hints.expected_shares',
    ...['text', 'integer', 'number', 'boolean', 'date', 'datetime'].map((k) => `models.form.kind.${k}`), 'models.choices.correction_ab.none']
  const holes = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((x) => x[1]).sort().join()
  for (const lang of LANGS) {
    for (const key of own) {
      assert.ok(WORDS[lang][key]?.trim(), `${lang} ${key}`)
      assert.equal(holes(WORDS[lang][key]), holes(WORDS.en[key]), `${lang} ${key}`)
    }
    // the hint of a field the form asks for is there for every key of both nodes (the form reads the hint of the model, else the shared one)
    for (const model of [AB, PLAN]) {
      for (const f of m.modelFields(model)) {
        const hint = WORDS[lang][`models.${model.id}.hints.${f.key}`] ?? WORDS[lang][`models.hints.${f.key}`]
        if (['alpha', 'output'].includes(f.key)) continue // the shared words every model has
        assert.ok(hint?.trim(), `${lang} ${model.id}.${f.key} has no hint`)
      }
    }
  }
  // the same reasons the catalogue gives, in the words of each language and not the English sentence of the engine
  for (const lang of LANGS.slice(1)) assert.notEqual(WORDS[lang]['models.reasons.ratio_needs_units'], WORDS.en['models.reasons.ratio_needs_units'], lang)
})

test('the form is drawn in every language with no word missing', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  for (const lang of LANGS) {
    const html = await render(Form, node('ab_test', { variant: 'arm', control: 'A', metric_type: 'ratio', sequential: true }), lang)
    const page = text(html)
    assert.ok(page.includes(WORDS[lang]['models.reasons.ratio_needs_units']), `${lang}: the reason`)
    assert.ok(page.includes(WORDS[lang]['models.params.expected_shares']), lang)
    assert.ok(page.includes(WORDS[lang]['models.ab_test.hint']), lang)
    assert.ok(!/models\.(params|hints|choice|form|reasons|ab_test)\./.test(page), `${lang}: a key shown for a word: ${page.match(/models\.[\w.]+/)}`)
    const plan = text(await render(Form, node('ab_plan', { metric_type: 'binary', metric: 'converted', solve_for: 'mde_for_days' }), lang))
    assert.ok(plan.includes(WORDS[lang]['models.ab_plan.hint']) && plan.includes(WORDS[lang]['models.params.days']), lang)
    assert.ok(!/models\.[a-z_]+\.[\w.]+/.test(plan), `${lang}: ${plan.match(/models\.[\w.]+/)}`)
  }
})
