// The Volatility (GARCH) and Risk simulation nodes in the editor (lot G5 of docs/specs/garch-risk.md), without a browser: the keys
// that change with `model` and with the variance model, the sections of the ARIMA and of the GARCH reused in the risk node, the
// quantiles list, «Register» hidden for the risk node, the action «Simulate risk from this model» (the node it makes has the
// source's options, the same in effect), the columns each node hands on before a Run, the icons and the summaries, the palette that
// still leaves both nodes out (their runners come with lots G1 and G3), and the words in the five languages.
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
const OUT = join(FRONTEND, 'tests', `.rendered-volatility-${process.pid}`)
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

const GARCH = m.modelOf('garch')
const RISK = m.modelOf('risk_simulation')
const ARIMA = m.modelOf('arima')
const COLUMNS = [{ name: 'day', dtype: 'Date' }, { name: 'close', dtype: 'Float64' }, { name: 'n', dtype: 'Int64' }, { name: 'shop', dtype: 'String' }]
const SERIES = { time: 'day', value: 'close', frequency: 'index' }
const keysOf = (model, params) => m.modelFields(model).filter((f) => m.modelApplies(model, f.key, params)).map((f) => f.key)
const sorted = (xs) => [...xs].sort()

// ── the keys change with what is chosen ──────────────────────────────────────

test('the volatility form: the keys of the variance model chosen, in the catalogue\'s order, the series first', () => {
  assert.equal(GARCH.card, true)
  assert.deepEqual(m.modelFields(GARCH).slice(0, 3).map((f) => f.key), ['time', 'value', 'frequency'])
  const common = ['time', 'value', 'frequency', 'input_kind', 'variance_model', 'arch_order', 'ar_order', 'ma_order', 'mean', 'distribution', 'se_type', 'horizon',
    'holdout', 'var_level', 'lb_lag', 'arch_lags', 'alpha', 'seed', 'output']
  assert.deepEqual(sorted(keysOf(GARCH, {})), sorted([...common, 'garch_order']))
  // an ARCH has no variance lags; prices ask how the returns are computed
  assert.deepEqual(sorted(keysOf(GARCH, { variance_model: 'arch' })), sorted(common))
  assert.deepEqual(sorted(keysOf(GARCH, { input_kind: 'prices' })), sorted([...common, 'garch_order', 'returns_type']))
  for (const v of ['gjr', 'egarch']) assert.ok(keysOf(GARCH, { variance_model: v }).includes('garch_order'), v)
  const by = Object.fromEntries(m.modelFields(GARCH).map((f) => [f.key, f]))
  assert.deepEqual(by.variance_model.choices, ['arch', 'garch', 'gjr', 'egarch'])
  assert.deepEqual(by.distribution.choices, ['norm', 'std', 'sstd'])
  assert.deepEqual([by.arch_order.min, by.arch_order.max, by.garch_order.min, by.garch_order.max, by.ar_order.max, by.ma_order.max], [1, 3, 0, 3, 5, 5])
  assert.deepEqual(by.lb_lag.choices, ['auto'])
  assert.ok(m.modelWhole(by.lb_lag))
})

test('a new volatility node starts with the spec\'s defaults, and a variance model with no lags of its own drops them', () => {
  const start = m.modelDefaults('garch')
  assert.deepEqual([start.variance_model, start.distribution, start.se_type, start.horizon, start.var_level, start.output], ['garch', 'std', 'robust', 10, 0.99, 'volatility'])
  assert.deepEqual([start.arch_order, start.garch_order, start.ar_order, start.ma_order, start.holdout, start.alpha, start.seed], [1, 1, 0, 0, 0, 0.05, 1])
  assert.equal(start.lb_lag, 'auto')
  assert.equal(start.input_kind, 'returns')
  assert.ok(!('returns_type' in start))
  let p = m.modelSet(GARCH, { ...start, ...SERIES }, 'variance_model', 'arch')
  assert.ok(!('garch_order' in p))
  assert.equal(p.arch_order, 1)
  p = m.modelSet(GARCH, p, 'input_kind', 'prices')
  assert.equal(m.modelFull(GARCH, p).returns_type, 'log')
  p = m.modelSet(GARCH, p, 'returns_type', 'simple')
  p = m.modelSet(GARCH, p, 'input_kind', 'returns')
  assert.ok(!('returns_type' in p), 'the way the returns are computed goes with the prices')
  assert.equal(p.time, 'day')
})

test('the risk form: the model chosen brings the keys of the GARCH or of the ARIMA, and the simulation\'s own stay', () => {
  assert.equal(RISK.card, false)
  const own = ['time', 'value', 'frequency', 'model', 'mean', 'paths', 'horizon', 'innovations', 'var_level', 'es_level', 'tail', 'quantiles', 'threshold', 'position', 'drawdown', 'holdout',
    'alpha', 'seed', 'lb_lag', 'arch_lags', 'output']
  const garchOnly = ['input_kind', 'variance_model', 'arch_order', 'garch_order', 'ar_order', 'ma_order', 'distribution', 'se_type']
  assert.deepEqual(sorted(keysOf(RISK, {})), sorted([...own, ...garchOnly]))
  const arimaOnly = ['s', 'orders', 'p', 'd', 'q', 'P', 'D', 'Q', 'drift', 'transform', 'aggregate']
  assert.deepEqual(sorted(keysOf(RISK, { model: 'arima' })), sorted([...own, ...arimaOnly]))
  // the GARCH's sections are the GARCH node's: the same keys, the same rules
  const sharedWithGarch = garchOnly.filter((k) => k in GARCH.takes)
  assert.deepEqual(sharedWithGarch, garchOnly)
  for (const k of garchOnly) assert.deepEqual(RISK.defaults[k], GARCH.defaults[k], k)
  assert.deepEqual(sorted(keysOf(RISK, { variance_model: 'arch' })), sorted([...own, ...garchOnly.filter((k) => k !== 'garch_order')]))
  assert.ok(keysOf(RISK, { input_kind: 'prices' }).includes('returns_type'))
  // the ARIMA's sections: the orders by hand or searched for, the transform's lambda
  const manual = keysOf(RISK, { model: 'arima' })
  assert.ok(['p', 'd', 'q', 'P', 'D', 'Q'].every((k) => manual.includes(k)) && !manual.includes('criterion') && !manual.includes('max_p'))
  const auto = keysOf(RISK, { model: 'arima', orders: 'auto' })
  assert.ok(['criterion', 'stepwise', 'max_p', 'max_q', 'max_P', 'max_Q', 'max_d', 'max_D', 'unit_root_test', 'seasonal_test'].every((k) => auto.includes(k)) && !auto.includes('p'))
  assert.ok(!manual.includes('box_cox_lambda') && keysOf(RISK, { model: 'arima', transform: 'box_cox' }).includes('box_cox_lambda'))
  // the ARIMA's keys are the ARIMA node's, and no exogenous column: its future values cannot be simulated
  for (const k of arimaOnly.filter((x) => x !== 'aggregate')) assert.ok(k in ARIMA.takes, k)
  assert.ok(!('exog' in RISK.takes))
  // changing the model drops the other one's keys
  let p = m.modelSet(RISK, { ...SERIES, variance_model: 'gjr', distribution: 'sstd', ar_order: 1 }, 'model', 'arima')
  for (const gone of ['variance_model', 'distribution', 'ar_order']) assert.ok(!(gone in p), gone)
  p = m.modelSet(RISK, { ...p, p: 2, d: 1, q: 1 }, 'model', 'garch')
  for (const gone of ['p', 'd', 'q']) assert.ok(!(gone in p), gone)
})

test('the risk node starts with the spec\'s defaults: 10,000 paths, ten steps, 0.99 and 0.975, a fall, seven quantiles, the summary', () => {
  const start = m.modelDefaults('risk_simulation')
  assert.deepEqual([start.model, start.paths, start.horizon, start.innovations, start.var_level, start.es_level, start.tail, start.drawdown, start.holdout, start.seed, start.output],
    ['garch', 10000, 10, 'parametric', 0.99, 0.975, 'lower', true, 0, 1, 'risk_summary'])
  assert.deepEqual(start.quantiles, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
  assert.ok(!('threshold' in start) && !('position' in start))
  const by = Object.fromEntries(m.modelFields(RISK).map((f) => [f.key, f]))
  assert.deepEqual(by.output.choices, ['risk_summary', 'paths', 'results'])
  assert.deepEqual(by.tail.choices, ['lower', 'upper'])
  assert.deepEqual(by.innovations.choices, ['parametric', 'bootstrap'])
  assert.deepEqual(by.aggregate.choices, ['last', 'sum', 'relative_change'])
  assert.deepEqual([by.paths.min, by.paths.max], [100, 10000000])
})

// ── the quantiles list, the levels, the threshold ────────────────────────────

test('the quantiles are a list of up to nine levels between 0 and 1; the levels are numbers above 0.5 and below 1', () => {
  const by = Object.fromEntries(m.modelFields(RISK).map((f) => [f.key, f]))
  assert.equal(by.quantiles.control, 'numbers')
  assert.equal(by.quantiles.max_count, 9)
  assert.equal(m.modelNumbersProblem(by.quantiles, [0.01, 0.5, 0.99]), '')
  assert.equal(m.modelNumbersProblem(by.quantiles, [0, 0.5]), 'between')
  assert.equal(m.modelNumbersProblem(by.quantiles, [0.5, 1]), 'between')
  assert.equal(m.modelNumbersProblem(by.quantiles, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]), 'tooMany')
  // whole numbers are not asked for here
  assert.ok(!m.modelWhole(by.quantiles))
  for (const key of ['var_level', 'es_level']) {
    assert.equal(m.modelNumberProblem(by[key], 0.975), '', key)
    assert.notEqual(m.modelNumberProblem(by[key], 0.5), '', key)
    assert.notEqual(m.modelNumberProblem(by[key], 1), '', key)
  }
  assert.equal(m.modelNumberProblem(by.position, 1000000), '')
  assert.notEqual(m.modelNumberProblem(by.position, 0), '')
  assert.notEqual(m.modelNumberProblem(by.position, -5), '')
  assert.equal(m.modelNumberProblem(by.threshold, -0.1), '')
  // the list the person writes is kept sorted, once each, as the engine reads it
  assert.deepEqual(m.modelSet(RISK, SERIES, 'quantiles', [0.95, 0.05, 0.5, 0.05]).quantiles, [0.05, 0.5, 0.95])
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
const node = (id, params = {}, more = {}) => ({ nodeId: `${id}-1`, data: { model: id, params, timeoutSeconds: 900, retries: 0, ...more }, inputColumns: COLUMNS })
const radio = (html, id, key, value) => html.match(new RegExp(`<input type="radio" name="model-${id}-1-${key}"[^>]*value="${value}"[^>]*>`))?.[0] ?? ''

test('a new volatility node: the form in its words, ARCH\'s lags and the prices\' returns drawn only when they apply', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node('garch', m.modelDefaults('garch')))
  const page = text(html)
  for (const words of ['Models how the spread of a return changes', 'Time column', 'Frequency of the series', 'The series holds', 'Variance model', 'Shock lags (q)', 'Variance lags (p)',
    'AR lags of the mean', 'MA lags of the mean', 'Mean (a constant)', 'Distribution', 'Standard errors', 'Forecast steps', 'Last returns kept out to test the VaR', 'VaR level',
    'ARCH test lags', 'Significance level', 'Seed', 'Output', 'Still to set: Time column, Series (a number), Frequency of the series.']) {
    assert.ok(page.includes(words), `«${words}» is not on the page: ${page}`)
  }
  assert.ok(!page.includes('Returns computed as'), 'the returns are not computed from prices here')
  // the choices are buttons: the variance model, the law, the standard errors, the input
  for (const v of ['arch', 'garch', 'gjr', 'egarch']) assert.ok(radio(html, 'garch', 'variance_model', v), v)
  assert.match(radio(html, 'garch', 'variance_model', 'garch'), /checked/)
  for (const v of ['norm', 'std', 'sstd']) assert.ok(radio(html, 'garch', 'distribution', v), v)
  assert.match(radio(html, 'garch', 'distribution', 'std'), /checked/)
  assert.match(radio(html, 'garch', 'se_type', 'robust'), /checked/)
  assert.match(radio(html, 'garch', 'input_kind', 'returns'), /checked/)
  assert.match(radio(html, 'garch', 'output', 'volatility'), /checked/)
  // the own words of the node, not another model's
  assert.ok(page.includes('The law of the shocks, standardised.') && !page.includes('How the times are spread'), page)
  assert.ok(page.includes('Steps ahead of the volatility forecast'), page)
  // ARCH: no variance lags; prices: the returns' type
  const arch = text(await render(Form, node('garch', { ...SERIES, variance_model: 'arch', input_kind: 'prices' })))
  assert.ok(!arch.includes('Variance lags (p)') && arch.includes('Shock lags (q)') && arch.includes('Returns computed as'), arch)
  assert.ok(!arch.includes('Still to set'), arch)
  // «Register» is there: the node writes a model
  const withFlow = text(await render(Form, node('garch', SERIES, { mlflow: { connectionId: 3, experiment: 'risk' } })))
  assert.ok(withFlow.includes('Register the model as') && !withFlow.includes('This node writes no model'), withFlow)
})

test('the risk node: the form of the model chosen, the simulation\'s own keys, and no model to register', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node('risk_simulation', m.modelDefaults('risk_simulation')))
  const page = text(html)
  for (const words of ['Fits a GARCH or an ARIMA model and simulates', 'Model of the series', 'Variance model', 'Distribution', 'Simulated paths', 'Forecast steps', 'Innovations',
    'VaR level', 'Expected shortfall level', 'Which side is the risk', 'Quantile levels', 'Level to watch', 'Position (an amount)', 'Maximum drawdown',
    'Rows kept out to test the forecast', 'Seed', 'Output']) {
    assert.ok(page.includes(words), `«${words}» is not on the page: ${page}`)
  }
  for (const gone of ['Seasonal period', 'Risk variable', 'Orders', 'Drift']) assert.ok(!page.includes(gone), gone)
  for (const v of ['garch', 'arima']) assert.ok(radio(html, 'risk_simulation', 'model', v), v)
  assert.match(radio(html, 'risk_simulation', 'model', 'garch'), /checked/)
  for (const v of ['lower', 'upper']) assert.ok(radio(html, 'risk_simulation', 'tail', v), v)
  assert.match(radio(html, 'risk_simulation', 'tail', 'lower'), /checked/)
  assert.match(radio(html, 'risk_simulation', 'innovations', 'parametric'), /checked/)
  assert.match(radio(html, 'risk_simulation', 'output', 'risk_summary'), /checked/)
  // the quantiles: a box of numbers, the default levels said back, sorted
  assert.match(html, /<input id="model-risk_simulation-1-quantiles" type="text"[^>]*placeholder="e\.g\. 0\.1, 0\.9"/)
  assert.ok(page.includes('7 values read: 0.01 · 0.05 · 0.25 · 0.5 · 0.75 · 0.95 · 0.99') || page.includes('Up to 9 levels between 0 and 1'), page)
  // an ARIMA: its own sections
  const arima = text(await render(Form, node('risk_simulation', { ...SERIES, model: 'arima', aggregate: 'sum' })))
  for (const words of ['Seasonal period', 'Risk variable', 'Drift', 'Transform the series']) assert.ok(arima.includes(words), `«${words}»: ${arima}`)
  for (const gone of ['Variance model', 'Shock lags (q)', 'Distribution', 'Standard errors']) assert.ok(!arima.includes(gone), gone)
  assert.ok(arima.includes('The running total'), arima)
  // a quantile out of range: the box says so
  const bad = text(await render(Form, node('risk_simulation', { ...SERIES, quantiles: [0.5, 1.2] })))
  assert.ok(bad.includes('A number between 0 and 1'), bad)
  // «Register the model as» is not drawn: the node writes no model
  const withFlow = text(await render(Form, node('risk_simulation', SERIES, { mlflow: { connectionId: 3, experiment: 'risk' } })))
  assert.ok(withFlow.includes('Experiment') && withFlow.includes('Continue without MLflow'), withFlow)
  assert.ok(!withFlow.includes('Register the model as') && withFlow.includes('This node writes no model'), withFlow)
})

test('the form is usable with the keyboard alone: native controls, each named, none that needs F, Home, End or Menu', () => {
  const form = read('../components/ui/ModelForm.vue')
  // nothing the two nodes add is a custom widget: they are drawn by the same native controls as the others
  assert.doesNotMatch(form, /@keydown|@keyup|tabindex="[1-9]/)
  assert.match(form, /<fieldset v-else-if="f\.control === 'choice' && asButtons\(f\)" class="field choices">/)
  assert.match(form, /<label :for="idOf\(f\.key\)" class="lbl">\{\{ labelOf\(f\) \}\}<\/label>\s*<input\s+:id="idOf\(f\.key\)"\s+type="text"/)
  // the ring's keys: the arrows, Enter, Space, Escape and Tab: no function key, no Menu
  const ring = read('../components/ui/FlowQuickMenu.vue')
  const keys = ring.slice(ring.indexOf('function onKey'), ring.indexOf('// a press anywhere else'))
  for (const k of ['Escape', 'ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Enter', "' '", 'Tab']) assert.ok(keys.includes(k), k)
  assert.doesNotMatch(keys, /'F\d+'|ContextMenu/)
})

// ── the action «Simulate risk from this model» ───────────────────────────────

const shared = (from, to) => [...Object.keys(to.needs), ...Object.keys(to.takes)].filter((k) => k !== 'model' && k !== 'output' && (k in from.needs || k in from.takes))

test('«Simulate risk from this model» on a GARCH node: the options of the node, the same in effect, and what is the simulation\'s own as the catalogue starts it', () => {
  const params = { ...SERIES, input_kind: 'prices', returns_type: 'simple', variance_model: 'gjr', arch_order: 2, garch_order: 1, ar_order: 1, ma_order: 0, mean: false,
    distribution: 'sstd', se_type: 'classical', horizon: 20, holdout: 250, var_level: 0.975, lb_lag: 15, alpha: 0.1, seed: 7, output: 'results' }
  const source = { model: 'garch', params, timeoutSeconds: 1800, retries: 2, mlflow: { connectionId: 3, experiment: 'x' }, label: 'my garch' }
  const made = m.modelRiskFrom(source)
  assert.ok('data' in made, JSON.stringify(made))
  const data = made.data
  assert.equal(data.model, 'risk_simulation')
  assert.equal(data.params.model, 'garch')
  // every option the two nodes share is the same in effect (a default the source relies on is carried as its value)
  const fromFull = m.modelFull(GARCH, m.modelParams(GARCH, params))
  const toFull = m.modelFull(RISK, data.params)
  for (const key of shared(GARCH, RISK)) {
    if (!m.modelApplies(GARCH, key, params)) continue
    assert.deepEqual(toFull[key], fromFull[key], key)
  }
  // …including the ones the source left to its defaults
  const bare = m.modelRiskFrom({ model: 'garch', params: SERIES }).data
  const bareFull = m.modelFull(RISK, bare.params)
  const bareFrom = m.modelFull(GARCH, SERIES)
  for (const key of shared(GARCH, RISK)) if (m.modelApplies(GARCH, key, SERIES)) assert.deepEqual(bareFull[key], bareFrom[key], key)
  // the simulation's own start as the catalogue says; `output` is its own
  assert.deepEqual([toFull.paths, toFull.es_level, toFull.tail, toFull.innovations, toFull.output], [10000, 0.975, 'lower', 'parametric', 'risk_summary'])
  assert.deepEqual(toFull.quantiles, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
  // the node's own time limit and retries; no MLflow, no label (the new node registers nothing)
  assert.deepEqual([data.timeoutSeconds, data.retries], [1800, 2])
  assert.ok(!('mlflow' in data) && !('label' in data))
  // nothing the source does not have is made up: the keys of an ARIMA are not there
  for (const gone of ['s', 'orders', 'p', 'drift', 'aggregate', 'transform']) assert.ok(!(gone in data.params), gone)
  // the node's source is not touched
  assert.equal(source.params.output, 'results')
})

test('«Simulate risk from this model» on an ARIMA node: the orders (by hand or searched for), the season, the transform, the horizon', () => {
  for (const params of [
    { ...SERIES, frequency: 'month', p: 1, d: 1, q: 1, P: 0, D: 1, Q: 1, s: 12, drift: false, transform: 'log', horizon: 24, holdout: 12, alpha: 0.1, seed: 3, output: 'forecast' },
    { ...SERIES, frequency: 'week', orders: 'auto', criterion: 'bic', stepwise: false, max_p: 3, max_q: 2, max_d: 1, unit_root_test: 'adf', seasonal_test: 'ocsb', transform: 'box_cox', box_cox_lambda: 0.5, mean: false },
    { ...SERIES },
  ]) {
    const made = m.modelRiskFrom({ model: 'arima', params, timeoutSeconds: 900, retries: 0 })
    assert.ok('data' in made)
    assert.equal(made.data.params.model, 'arima')
    const fromFull = m.modelFull(ARIMA, m.modelParams(ARIMA, params))
    const toFull = m.modelFull(RISK, made.data.params)
    let compared = 0
    for (const key of shared(ARIMA, RISK)) {
      if (!m.modelApplies(ARIMA, key, params)) continue
      assert.deepEqual(toFull[key], fromFull[key], key)
      compared++
    }
    assert.ok(compared >= 12, `${compared} options compared`)
    // the horizon an ARIMA relies on (12) is carried, not the simulation's 10
    assert.equal(toFull.horizon, fromFull.horizon)
    // the keys the form draws for the new node are the ones of an ARIMA, and nothing of a GARCH
    const drawn = keysOf(RISK, made.data.params)
    assert.ok(drawn.includes('aggregate') && !drawn.includes('variance_model') && !drawn.includes('distribution'))
    assert.deepEqual(toFull.aggregate, 'last')
  }
  // what is ready to run in the source is ready in the copy (the series' columns come with it)
  const ready = m.modelRiskFrom({ model: 'arima', params: { ...SERIES, p: 1 } }).data
  assert.deepEqual(m.modelUnset(RISK, ready.params), [])
  assert.deepEqual(m.modelUnset(RISK, m.modelRiskFrom({ model: 'garch', params: {} }).data.params), ['time', 'value', 'frequency'])
})

test('the action is refused for what cannot be simulated, and offered only on the two nodes', () => {
  // an ARIMA with exogenous columns: their future values cannot be simulated
  assert.deepEqual(m.modelRiskFrom({ model: 'arima', params: { ...SERIES, exog: ['n'] } }), { refused: 'exog' })
  assert.ok('data' in m.modelRiskFrom({ model: 'arima', params: { ...SERIES, exog: [] } }))
  for (const other of ['linear', 'decomposition', 'timestral', 'risk_simulation', 'ab_test', 'nothing']) assert.deepEqual(m.modelRiskFrom({ model: other, params: {} }), { refused: 'model' }, other)
  assert.deepEqual(m.modelRiskFrom(null), { refused: 'model' })
  assert.deepEqual(m.MODEL_RISK_FROM, ['arima', 'garch'])
  // the actions of a node of the canvas
  assert.deepEqual(icons.modelActionsOf({ type: 'model', data: { model: 'arima' } }), ['simulate_risk'])
  assert.deepEqual(icons.modelActionsOf({ type: 'model', data: { model: 'garch' } }), ['simulate_risk'])
  for (const n of [{ type: 'model', data: { model: 'risk_simulation' } }, { type: 'model', data: { model: 'linear' } }, { type: 'model_predict', data: { model: 'arima' } },
    { type: 'operation', data: { model: 'garch' } }, null, undefined]) assert.deepEqual(icons.modelActionsOf(n), [])
  assert.equal(icons.MODEL_ACTIONS.simulate_risk.label, 'models.actions.simulateRisk')
})

test('the editor opens the action from the node\'s menu, on the same input, as a branch of its own', () => {
  const editor = read('../components/FlowEditor.vue')
  const ring = read('../components/ui/FlowQuickMenu.vue')
  // the ring gets the node's actions after the steps, and says `action` for one
  assert.match(editor, /<FlowQuickMenu ref="quickMenu" :items="shortcutItems" :actions="quickActions" @pick="addOperationAt" @action="onQuickAction"/)
  assert.match(ring, /\(e: 'action', id: ModelActionId\): void/)
  assert.match(ring, /if \(entry\.action\) emit\('action', entry\.action\)/)
  // the actions are those of the node the menu was opened on: a right click on it, the keyboard's menu on the selected one
  assert.match(editor, /setQuickActions\(beside\)/)
  assert.match(editor, /setQuickActions\(parent\?\.id\)/)
  const action = editor.slice(editor.indexOf('function onQuickAction'), editor.indexOf('function onQuickMenuClose'))
  // the node reads the same input as the source (its upstream), one more branch there, with the options copied
  assert.match(action, /modelRiskFrom\(source\.data\)/)
  assert.match(action, /chainNeighbour\(chainNodes\(\), chainEdges\(\), source\.id, 'upstream'\)/)
  assert.match(action, /branch: true/)
  assert.match(action, /type: MODEL_NODE_TYPE, data: made\.data/)
  // what cannot be done says why, with the catalogue's words, and an observer cannot
  assert.match(action, /models\.actions\.simulateRiskRefused\.\$\{made\.refused\}/)
  assert.match(action, /models\.actions\.simulateRiskNoInput/)
  assert.match(action, /deniedToObserver\(\)/)
  // the canvas, the panel and the table's header name the nodes by the same function
  for (const file of ['../components/FlowEditor.vue', '../components/nodes/ModelNode.vue', '../components/models/ModelNodePanel.vue']) assert.match(read(file), /modelCodeTitle\(/, file)
})

// ── titles, icons, summaries, the palette ────────────────────────────────────

test('a volatility node is named by its variance model, as the engine\'s report names it; the risk node by its model', () => {
  assert.equal(m.modelGarchTitle({}), 'GARCH(1,1), t')
  assert.equal(m.modelGarchTitle({ variance_model: 'gjr', distribution: 'sstd' }), 'GJR-GARCH(1,1), skewed t')
  assert.equal(m.modelGarchTitle({ variance_model: 'egarch', distribution: 'norm', ar_order: 1 }), 'AR(1)-EGARCH(1,1), normal')
  assert.equal(m.modelGarchTitle({ variance_model: 'arch', arch_order: 2 }), 'ARCH(2), t')
  // p lags of the variance first, then q of the shocks
  assert.equal(m.modelGarchTitle({ garch_order: 2, arch_order: 3 }), 'GARCH(2,3), t')
  assert.equal(m.modelGarchTitle({ ar_order: 1, ma_order: 2 }), 'ARMA(1,2)-GARCH(1,1), t')
  assert.equal(m.modelGarchTitle({ ma_order: 1 }), 'MA(1)-GARCH(1,1), t')
  assert.equal(m.modelCodeTitle('garch', { variance_model: 'gjr' }), 'GJR-GARCH(1,1), t')
  assert.equal(m.modelCodeTitle('arima', { p: 1, d: 1, q: 1 }), 'ARIMA(1,1,1)')
  assert.equal(m.modelCodeTitle('linear', {}), null)
  assert.equal(m.modelCodeTitle('risk_simulation', {}), null)
  // the canvas' line
  assert.equal(m.modelSummary('garch', { ...SERIES }), 'GARCH(1,1), t · close')
  assert.equal(m.modelSummary('garch', {}), 'GARCH(1,1), t')
  assert.equal(m.modelSummary('risk_simulation', { ...SERIES }), 'GARCH(1,1), t · close · N=10,000')
  assert.equal(m.modelSummary('risk_simulation', { ...SERIES, model: 'arima', p: 2, d: 1, q: 0, paths: 250000 }), 'ARIMA(2,1,0) · close · N=250,000')
})

test('the two nodes have icons of their own, and the palette still leaves them out until their runners are built; with them in, each is one entry under Time series', () => {
  assert.notEqual(icons.modelIconOf('garch'), icons.modelIconOf('arima'))
  assert.notEqual(icons.modelIconOf('risk_simulation'), icons.modelIconOf('arima'))
  assert.notEqual(icons.modelIconOf('garch'), icons.modelIconOf('risk_simulation'))
  assert.notEqual(icons.modelIconOf('garch'), icons.modelIconOf('decomposition'))
  assert.notEqual(icons.modelIconOf('risk_simulation'), icons.modelIconOf('linear'))
  const t = (k) => WORDS.en[k] ?? k
  const shown = m.MODEL_CATALOG.groups.filter((g) => g.id === 'timeseries')
  // hidden by default (lots G1 and G3 give them their runners, lot G7 takes them off this list)
  assert.deepEqual([...m.MODEL_NOT_IN_THE_PALETTE_YET].filter((id) => id === 'garch' || id === 'risk_simulation').sort(), ['garch', 'risk_simulation'])
  const hidden = m.modelPalette(shown, '', t)
  assert.ok(!hidden.flatMap((g) => g.items).some((i) => i.model === 'garch' || i.model === 'risk_simulation'))
  // shown: the two entries, in the catalogue's order after the decomposition, found by their names
  const open = m.modelPalette(shown, '', t, m.MODEL_CATALOG, new Set())
  assert.deepEqual(open.map((g) => g.group), ['timeseries'])
  const items = open[0].items
  const ids = items.map((i) => i.model)
  assert.equal(ids.filter((x) => x === 'garch').length, 1)
  assert.equal(ids.filter((x) => x === 'risk_simulation').length, 1)
  assert.ok(ids.indexOf('garch') > ids.indexOf('decomposition') && ids.indexOf('risk_simulation') === ids.indexOf('garch') + 1)
  const entry = (id) => items.find((i) => i.model === id)
  assert.equal(entry('garch').kind, 'model:garch')
  assert.equal(entry('risk_simulation').kind, 'model:risk_simulation')
  assert.equal(entry('garch').preset, undefined)
  assert.equal(t(entry('garch').nameKey), 'Volatility (GARCH)')
  assert.equal(t(entry('risk_simulation').nameKey), 'Risk simulation')
  assert.equal(m.modelPalette(shown, 'volatility (g', t, m.MODEL_CATALOG, new Set())[0].items.length, 1)
  assert.equal(m.modelPalette(shown, 'risk simulation', t, m.MODEL_CATALOG, new Set())[0].items.length, 1)
  // a node dropped from the palette starts from the defaults
  assert.deepEqual(m.modelFromKind('model:garch'), { model: 'garch' })
  assert.equal(m.modelNodeData('risk_simulation').params.paths, 10000)
})

// ── the columns before the Run ───────────────────────────────────────────────

test('the columns the two nodes hand on are known before the Run, as the form edits them, and the steps after them keep them', () => {
  const names = (model, params, input = COLUMNS) => m.modelOutputColumns(model, params, input).columns.map((c) => c.name)
  // volatility: the table; the returns' column with prices only
  assert.deepEqual(names(GARCH, {}), ['time', 'observed', 'mean', 'sigma', 'std_residual', 'var', 'violation', 'is_holdout', 'is_forecast'])
  assert.deepEqual(names(GARCH, { input_kind: 'prices' }).slice(0, 4), ['time', 'observed', 'return', 'mean'])
  assert.equal(m.modelOutputColumns(GARCH, { output: 'results' }, []).columns.length, 10)
  // the risk summary: a quantile column for each level the box holds, the prices' columns with prices, the threshold's with a threshold
  let p = m.modelSet(RISK, m.modelDefaults('risk_simulation'), 'time', 'day')
  p = m.modelSet(RISK, p, 'value', 'close')
  assert.deepEqual(names(RISK, p).filter((n) => n.startsWith('quantile_')), ['quantile_0.01', 'quantile_0.05', 'quantile_0.25', 'quantile_0.5', 'quantile_0.75', 'quantile_0.95', 'quantile_0.99'])
  p = m.modelSet(RISK, p, 'quantiles', [0.95, 0.05])
  assert.deepEqual(names(RISK, p).filter((n) => n.startsWith('quantile_')), ['quantile_0.05', 'quantile_0.95'])
  assert.ok(!names(RISK, p).includes('prob_crossed') && !names(RISK, p).includes('price_mean'))
  p = m.modelSet(RISK, p, 'threshold', 100)
  assert.ok(names(RISK, p).includes('prob_crossed'))
  p = m.modelSet(RISK, p, 'input_kind', 'prices')
  assert.ok(names(RISK, p).includes('price_mean') && names(RISK, p).includes('price_quantile_0.05'))
  p = m.modelSet(RISK, p, 'model', 'arima')
  assert.ok(!names(RISK, p).includes('price_mean'))
  // the paths: a column for each, the sigma only with a GARCH
  assert.deepEqual(names(RISK, { output: 'paths' }), ['path', 'step', 'time', 'value', 'cumulative', 'sigma'])
  assert.deepEqual(names(RISK, { output: 'paths', model: 'arima' }), ['path', 'step', 'time', 'value', 'cumulative'])
  // the time is the input column's own type
  const out = m.modelOutputColumns(RISK, { time: 'day', output: 'paths' }, COLUMNS)
  assert.equal(out.columns[2].dtype, 'Date')
  assert.deepEqual(out.later, [])
  // the steps after them: a select, a rename, a drop
  const first = m.modelOutputColumns(RISK, m.modelSet(RISK, m.modelDefaults('risk_simulation'), 'time', 'day'), COLUMNS).columns
  assert.deepEqual(m.modelColumnsAfterStep('select', { columns: ['time', 'var', 'es', 'gone'] }, first).map((c) => c.name), ['time', 'var', 'es'])
  assert.ok(m.modelColumnsAfterStep('rename', { mapping: { var: 'value_at_risk' } }, first).some((c) => c.name === 'value_at_risk'))
  assert.ok(!m.modelColumnsAfterStep('drop', { columns: ['es'] }, first).some((c) => c.name === 'es'))
  // the editor asks for them with no run, in the panel and in the table's header, for every model of the catalogue
  const editor = read('../components/FlowEditor.vue')
  assert.match(editor, /modelOutputColumns\(def, model\.data\?\.params, input\)/)
  assert.match(editor, /def = model\?\.type === MODEL_NODE_TYPE && !scriptSources\.value\[model\.id\] \? modelOf\(model\.data\?\.model\) : null/)
})

// ── the words ────────────────────────────────────────────────────────────────

test('every word of the two forms is in the five languages, with the same holes, and nothing is left in English', () => {
  const wanted = m.modelWordKeys({ ...m.MODEL_CATALOG, models: [GARCH, RISK] }).filter((k) => !k.startsWith('predict.'))
  assert.ok(wanted.length >= 50 && wanted.includes('models.params.variance_model') && wanted.includes('models.choice.sstd'), String(wanted.length))
  const holes = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((x) => x[1]).sort().join()
  const own = ['models.actions.simulateRisk', 'models.actions.simulateRiskNoInput', 'models.actions.simulateRiskRefused.model', 'models.actions.simulateRiskRefused.exog',
    'models.garch.params.holdout', 'models.risk_simulation.params.quantiles', 'models.risk_simulation.params.threshold', 'models.choices.tail.lower', 'models.choices.tail.upper',
    'models.choices.aggregate.last', 'models.choices.aggregate.sum', 'models.choices.aggregate.relative_change']
  for (const lang of LANGS) {
    for (const key of [...wanted, ...own]) {
      assert.ok(WORDS[lang][key]?.trim(), `${lang} ${key}`)
      assert.equal(holes(WORDS[lang][key]), holes(WORDS.en[key]), `${lang} ${key}`)
    }
    // a hint for every key the two nodes add, the own ones where the shared words say another thing
    for (const [model, id] of [[GARCH, 'garch'], [RISK, 'risk_simulation']]) {
      for (const f of m.modelFields(model)) {
        if (!['input_kind', 'returns_type', 'variance_model', 'arch_order', 'garch_order', 'ar_order', 'ma_order', 'se_type', 'var_level', 'arch_lags', 'model', 'aggregate', 'paths',
          'innovations', 'es_level', 'tail', 'position', 'drawdown', 'mean', 'distribution', 'lb_lag', 'holdout', 'horizon', 'seed', 'output', 'quantiles', 'threshold'].includes(f.key)) continue
        if (id === 'garch' && ['model', 'aggregate', 'paths', 'innovations', 'es_level', 'tail', 'position', 'drawdown', 'quantiles', 'threshold'].includes(f.key)) continue
        const hint = WORDS[lang][`models.${id}.hints.${f.key}`] ?? WORDS[lang][`models.hints.${f.key}`]
        assert.ok(hint?.trim(), `${lang} ${id}.${f.key} has no hint`)
      }
    }
  }
  // the hints the other models' words would say wrongly are the nodes' own
  for (const id of ['garch', 'risk_simulation']) for (const k of ['mean', 'distribution', 'lb_lag', 'holdout', 'horizon', 'seed', 'output']) assert.ok(WORDS.en[`models.${id}.hints.${k}`], `${id} ${k}`)
  assert.ok(WORDS.en['models.risk_simulation.hints.quantiles'] && WORDS.en['models.risk_simulation.hints.threshold'])
  // the sentences are in the language, not the engine's English
  for (const lang of LANGS.slice(1)) {
    for (const key of ['models.garch.name', 'models.garch.hint', 'models.risk_simulation.name', 'models.risk_simulation.hint', 'models.hints.variance_model', 'models.hints.paths', 'models.hints.tail',
      'models.params.paths', 'models.params.tail', 'models.params.position', 'models.choices.tail.lower', 'models.actions.simulateRisk', 'models.actions.simulateRiskNoInput',
      'models.actions.simulateRiskRefused.exog', 'models.garch.hints.output', 'models.risk_simulation.hints.threshold']) assert.notEqual(WORDS[lang][key], WORDS.en[key], `${lang} ${key}`)
  }
  // the keys the form and the editor ask for by name are all there
  for (const file of ['../components/FlowEditor.vue', '../components/ui/FlowQuickMenu.vue', '../composables/modelIcons.ts']) {
    for (const x of read(file).matchAll(/'(models\.actions\.[\w.]+)'/g)) assert.ok(WORDS.en[x[1]], x[1])
  }
})

test('the forms and the menu\'s action are drawn in every language with no word missing', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  for (const lang of LANGS) {
    const garch = text(await render(Form, node('garch', { ...SERIES, input_kind: 'prices', variance_model: 'gjr' }), lang))
    assert.ok(garch.includes(WORDS[lang]['models.garch.hint']), lang)
    for (const k of ['models.params.variance_model', 'models.params.input_kind', 'models.params.returns_type', 'models.params.garch_order', 'models.choice.gjr', 'models.choice.sstd', 'models.garch.params.holdout']) {
      assert.ok(garch.includes(WORDS[lang][k]), `${lang} ${k}`)
    }
    const risk = text(await render(Form, node('risk_simulation', SERIES), lang))
    for (const k of ['models.risk_simulation.hint', 'models.params.model', 'models.params.paths', 'models.params.es_level', 'models.params.tail', 'models.risk_simulation.params.quantiles',
      'models.risk_simulation.params.threshold', 'models.params.position', 'models.params.drawdown', 'models.choices.tail.lower', 'models.choices.tail.upper', 'models.choice.bootstrap']) {
      assert.ok(risk.includes(WORDS[lang][k]), `${lang} ${k}`)
    }
    const arima = text(await render(Form, node('risk_simulation', { ...SERIES, model: 'arima' }), lang))
    assert.ok(arima.includes(WORDS[lang]['models.params.aggregate']) && arima.includes(WORDS[lang]['models.choices.aggregate.last']), lang)
    for (const html of [garch, risk, arima]) assert.ok(!/models\.[a-z_]+\.[\w.]+/.test(html), `${lang}: a key shown for a word: ${html.match(/models\.[\w.]+/)}`)
  }
})
