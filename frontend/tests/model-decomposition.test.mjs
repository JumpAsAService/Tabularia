// The decomposition node in the editor (lot D5 of docs/specs/ts-decomposition.md), without a browser: the keys that change with
// the method (STL, classical, MSTL, EMD), the list of the periods and its whole numbers, the choices that are `auto`, a multiplicative
// decomposition with a transform shown grey with its reason, no model to register (the node writes none), the columns it hands on
// before it has run (`seasonal_7`, `imf_1…`) down to the steps after it, the icon, the summary, the palette that still leaves the node
// out (its runner refuses «not built yet» until lots D2 and D3), and the words in the five languages.
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
const OUT = join(FRONTEND, 'tests', `.rendered-decomposition-${process.pid}`)
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

const DEC = m.modelOf('decomposition')
const COLUMNS = [
  { name: 'day', dtype: 'Date' }, { name: 'orders', dtype: 'Float64' }, { name: 'n', dtype: 'Int64' }, { name: 'shop', dtype: 'String' },
]
const keysOf = (params) => m.modelFields(DEC).filter((f) => m.modelApplies(DEC, f.key, params)).map((f) => f.key)
const COMMON = ['time', 'value', 'frequency', 'method', 'transform', 'missing', 'seasonality_test', 'lb_lag', 'alpha', 'output']

// ── the keys change with the method ──────────────────────────────────────────

test('the form of the decomposition: the keys of the method chosen, in the catalogue\'s order', () => {
  assert.deepEqual(DEC.card, false)
  const sorted = (xs) => [...xs].sort()
  assert.deepEqual(sorted(keysOf({})), sorted([...COMMON, 'period', 'robust', 's_window', 't_window']))
  assert.deepEqual(sorted(keysOf({ method: 'classical' })), sorted([...COMMON, 'period', 'type']))
  assert.deepEqual(sorted(keysOf({ method: 'mstl' })), sorted([...COMMON, 'periods', 'robust', 's_windows', 't_window', 'iterate']))
  assert.deepEqual(sorted(keysOf({ method: 'emd' })), sorted([...COMMON, 'period', 'max_imf', 's_number', 'max_siftings', 'noise_imfs']))
  // the Box–Cox lambda only with Box–Cox
  assert.ok(!keysOf({}).includes('box_cox_lambda'))
  assert.ok(keysOf({ transform: 'box_cox' }).includes('box_cox_lambda'))
  assert.ok(!keysOf({ transform: 'log' }).includes('box_cox_lambda'))
  // what the form draws: the time and the value first, as the catalogue's `needs`
  assert.deepEqual(m.modelFields(DEC).slice(0, 3).map((f) => f.key), ['time', 'value', 'frequency'])
  const by = Object.fromEntries(m.modelFields(DEC).map((f) => [f.key, f]))
  assert.equal(by.periods.control, 'numbers')
  assert.equal(by.periods.max_count, 4)
  assert.equal(by.period.control, 'integer')
  assert.equal(by.s_window.control, 'number_or_choice')
  assert.deepEqual(by.t_window.choices, ['auto'])
  assert.deepEqual(by.lb_lag.choices, ['auto'])
  assert.deepEqual(by.box_cox_lambda.choices, ['auto'])
})

test('a new decomposition starts with the spec\'s defaults, and what is saved holds only the keys that apply', () => {
  const start = m.modelDefaults('decomposition')
  assert.equal(start.method, 'stl')
  assert.equal(start.s_window, 11)
  assert.equal(start.t_window, 'auto')
  assert.equal(start.lb_lag, 'auto')
  assert.equal(start.alpha, 0.05)
  assert.equal(start.output, 'components')
  // the method changed: the keys of the other methods go, and the common ones stay
  let p = m.modelSet(DEC, { ...start, time: 'day', value: 'orders', frequency: 'day', robust: true }, 'method', 'emd')
  assert.equal(p.method, 'emd')
  for (const gone of ['robust', 's_window', 't_window', 'type', 'periods']) assert.ok(!(gone in p), gone)
  // (what is not written is the catalogue's default, shown in the box)
  assert.deepEqual([m.modelFull(DEC, p).max_imf, m.modelFull(DEC, p).s_number, m.modelFull(DEC, p).max_siftings], [10, 4, 50])
  assert.equal(p.time, 'day')
  p = m.modelSet(DEC, p, 'method', 'mstl')
  assert.ok(!('max_imf' in p) && !('s_number' in p))
  assert.equal(m.modelFull(DEC, p).iterate, 2)
  p = m.modelSet(DEC, p, 'periods', [365, 7])
  assert.deepEqual(p.periods, [7, 365])
  p = m.modelSet(DEC, p, 'method', 'classical')
  assert.ok(!('periods' in p) && !('iterate' in p))
  assert.equal(m.modelFull(DEC, p).type, 'additive')
})

test('a period for a yearly or index series, the periods of an MSTL: what the node still lacks is said with the catalogue\'s reasons', () => {
  const base = { time: 'day', value: 'orders' }
  assert.deepEqual(m.modelUnset(DEC, { ...base, frequency: 'day' }), [])
  assert.deepEqual(m.modelUnset(DEC, { ...base, frequency: 'year' }), ['period'])
  assert.deepEqual(m.modelUnset(DEC, { ...base, frequency: 'index', method: 'classical' }), ['period'])
  assert.deepEqual(m.modelUnset(DEC, { ...base, frequency: 'year', period: 4 }), [])
  // EMD needs no period; MSTL needs its periods whatever the frequency
  assert.deepEqual(m.modelUnset(DEC, { ...base, frequency: 'year', method: 'emd' }), [])
  assert.deepEqual(m.modelUnset(DEC, { ...base, frequency: 'day', method: 'mstl' }), ['periods'])
  assert.deepEqual(m.modelUnset(DEC, { ...base, frequency: 'day', method: 'mstl', periods: [7, 365] }), [])
  assert.equal(m.modelRequiredReason(DEC, 'period', { frequency: 'year' })?.reason, 'period_needed')
  assert.equal(m.modelRequiredReason(DEC, 'periods', { method: 'mstl' })?.reason, 'periods_needed')
  assert.equal(m.modelRequiredReason(DEC, 'period', { frequency: 'month' }), null)
})

// ── the periods, the spans and the lag ───────────────────────────────────────

test('the periods are whole numbers between 2 and the limit, at most four; the spans from 7 and 3; the lag from 1', () => {
  const by = Object.fromEntries(m.modelFields(DEC).map((f) => [f.key, f]))
  assert.equal(m.modelNumbersProblem(by.periods, [7, 365]), '')
  assert.equal(m.modelNumbersProblem(by.periods, [7, 365.25]), 'whole')
  assert.equal(m.modelNumbersProblem(by.periods, [1, 7]), 'range')
  assert.equal(m.modelNumbersProblem(by.periods, [7, 10001]), 'range')
  assert.equal(m.modelNumbersProblem(by.periods, [2, 3, 4, 5, 6]), 'tooMany')
  assert.equal(m.modelNumbersProblem(by.s_windows, [11, 15]), '')
  assert.equal(m.modelNumbersProblem(by.s_windows, [5]), 'atLeast')
  assert.equal(m.modelNumbersProblem(by.s_windows, [11.5]), 'whole')
  assert.equal(m.modelNumberProblem({ ...by.period }, 12), '')
  assert.equal(m.modelNumberProblem({ ...by.period }, 12.5), 'whole')
  assert.equal(m.modelNumberProblem({ ...by.period }, 1), 'range')
  // the number-or-word ones: whole too, and from their own lowest
  for (const [key, bad, good] of [['s_window', 5, 11], ['t_window', 2, 3], ['lb_lag', 0, 20]]) {
    assert.ok(m.modelWhole(key), key)
    assert.notEqual(m.modelNumberProblem({ ...by[key], control: 'integer' }, bad), '', key)
    assert.equal(m.modelNumberProblem({ ...by[key], control: 'integer' }, good), '', key)
  }
  assert.ok(!m.modelWhole('box_cox_lambda') && !m.modelWhole('alpha') && !m.modelWhole('quantiles'))
  // the other models' lists are not asked for whole numbers
  assert.equal(m.modelNumbersProblem({ key: 'quantiles', above: 0, below: 1 }, [0.25, 0.5]), '')
})

// ── a combination that is not allowed ────────────────────────────────────────

test('a multiplicative decomposition with a transform is grey with its reason, whichever of the two is chosen first', () => {
  const classical = { method: 'classical' }
  // multiplicative chosen: log and Box–Cox are off
  for (const t of ['log', 'box_cox']) assert.equal(m.modelForbiddenOption(DEC, 'transform', t, { ...classical, type: 'multiplicative' })?.reason, 'multiplicative_transform', t)
  assert.equal(m.modelForbiddenOption(DEC, 'transform', 'none', { ...classical, type: 'multiplicative' }), null)
  // a transform chosen: multiplicative is off
  for (const t of ['log', 'box_cox']) assert.equal(m.modelForbiddenOption(DEC, 'type', 'multiplicative', { ...classical, transform: t })?.reason, 'multiplicative_transform', t)
  assert.equal(m.modelForbiddenOption(DEC, 'type', 'additive', { ...classical, transform: 'log' }), null)
  assert.equal(m.modelForbiddenOption(DEC, 'type', 'multiplicative', classical), null)
  // only with the classical method
  assert.equal(m.modelForbiddenOption(DEC, 'transform', 'log', { method: 'stl', type: 'multiplicative' }), null)
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
const node = (params = {}, more = {}) => ({ nodeId: 'dec-1', data: { model: 'decomposition', params, timeoutSeconds: 900, retries: 0, ...more }, inputColumns: COLUMNS })
const radio = (html, key, value) => html.match(new RegExp(`<input type="radio" name="model-dec-1-${key}"[^>]*value="${value}"[^>]*>`))?.[0] ?? ''
const SERIES = { time: 'day', value: 'orders', frequency: 'day' }

test('a new decomposition: the STL form in its words, the keys of the other methods not drawn', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node(m.modelDefaults('decomposition')))
  const page = text(html)
  for (const words of ['Splits a series into trend', 'Time column', 'Series (a number)', 'Frequency of the series', 'Method', 'Seasonal period', 'Robust to outliers',
    'Seasonal smoother span', 'Trend smoother span', 'Transform the series', 'A period without a value', 'Test of the seasonality', 'Ljung–Box lag',
    'Significance level', 'Output', 'Still to set: Time column, Series (a number), Frequency of the series.']) {
    assert.ok(page.includes(words), `«${words}» is not on the page: ${page}`)
  }
  for (const gone of ['Seasonal periods', 'Components combine', 'Passes over the periods', 'Most IMFs to extract', 'Box–Cox λ']) assert.ok(!page.includes(gone), gone)
  // the method, the missing values and the test are buttons; the spans are a choice among words and a value
  for (const v of ['stl', 'classical', 'mstl', 'emd']) assert.ok(radio(html, 'method', v), v)
  assert.match(radio(html, 'method', 'stl'), /checked/)
  for (const v of ['refuse', 'linear', 'seasonal']) assert.ok(radio(html, 'missing', v), v)
  assert.match(radio(html, 'missing', 'refuse'), /checked/)
  assert.match(radio(html, 'seasonality_test', 'friedman'), /checked/)
  // s_window: «Periodic» or a value, the value 11 chosen; t_window and lb_lag: Automatic
  assert.match(html, /<input type="radio" name="model-dec-1-s_window"[^>]*>\s*<span>Periodic/)
  assert.match(html, /<input type="number"[^>]*value="11"[^>]*aria-label="Seasonal smoother span: A value"/)
  assert.match(html, /<input type="radio" name="model-dec-1-t_window"[^>]*checked[^>]*>\s*<span>Automatic/)
  assert.match(html, /<input type="radio" name="model-dec-1-lb_lag"[^>]*checked[^>]*>\s*<span>Automatic/)
})

test('the method decides the form: MSTL asks for its periods in a box of numbers, EMD for its IMFs, classical for the way the components combine', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const mstl = await render(Form, node({ ...SERIES, method: 'mstl' }))
  let page = text(mstl)
  for (const words of ['Seasonal periods', 'Seasonal smoother spans', 'Passes over the periods', 'Trend smoother span', 'Robust to outliers',
    'Still to set: Seasonal periods.', 'An MSTL decomposition needs its seasonal periods, in steps.']) assert.ok(page.includes(words), `«${words}»: ${page}`)
  for (const gone of ['Seasonal period ', 'Seasonal smoother span ', 'Most IMFs to extract', 'Components combine']) assert.ok(!page.includes(gone), gone)
  assert.match(mstl, /<input id="model-dec-1-periods" type="text"[^>]*placeholder="e\.g\. 7, 365"/)
  // the periods given: said back, sorted as the engine takes them, with no «still to set»
  const given = await render(Form, node({ ...SERIES, method: 'mstl', periods: [7, 365] }))
  page = text(given)
  assert.ok(page.includes('2 values read: 7 · 365'), page)
  assert.ok(!page.includes('Still to set') && !page.includes('needs its seasonal periods'))
  assert.match(given, /<input id="model-dec-1-periods"[^>]*value="7, 365"/)
  // a period that is not whole: the box says so
  const bad = text(await render(Form, node({ ...SERIES, method: 'mstl', periods: [7, 365.25] })))
  assert.ok(bad.includes('A whole number.'), bad)
  // EMD
  const emd = text(await render(Form, node({ ...SERIES, method: 'emd' })))
  for (const words of ['Most IMFs to extract', 'S-number (stopping rule)', 'Most siftings per IMF', 'First IMFs read as noise', 'Seasonal period']) assert.ok(emd.includes(words), words)
  for (const gone of ['Robust to outliers', 'Trend smoother span', 'Passes over the periods', 'Components combine']) assert.ok(!emd.includes(gone), gone)
  // classical
  const classical = text(await render(Form, node({ ...SERIES, method: 'classical' })))
  assert.ok(classical.includes('Components combine') && classical.includes('Additive') && classical.includes('Multiplicative'))
  for (const gone of ['Robust to outliers', 'Seasonal smoother span', 'Trend smoother span']) assert.ok(!classical.includes(gone), gone)
  // a yearly series: the period is asked for, with the reason
  const yearly = text(await render(Form, node({ ...SERIES, frequency: 'year' })))
  assert.ok(yearly.includes('Still to set: Seasonal period.') && yearly.includes('A yearly or index series has no season of its own: give the period, in steps.'), yearly)
  // Box–Cox: its lambda, Automatic
  const box = await render(Form, node({ ...SERIES, transform: 'box_cox' }))
  assert.ok(text(box).includes('Box–Cox λ'))
  assert.match(box, /<input type="radio" name="model-dec-1-box_cox_lambda"[^>]*checked[^>]*>\s*<span>Automatic/)
})

test('multiplicative with a transform is grey on the form with its reason, one against the other', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const REASON = 'A multiplicative decomposition already works in ratios'
  let html = await render(Form, node({ ...SERIES, method: 'classical', type: 'multiplicative' }))
  assert.match(radio(html, 'transform', 'log'), /disabled/)
  assert.match(radio(html, 'transform', 'box_cox'), /disabled/)
  assert.ok(!/disabled/.test(radio(html, 'transform', 'none')))
  assert.ok(text(html).includes(REASON), 'the reason is on the page')
  html = await render(Form, node({ ...SERIES, method: 'classical', transform: 'log' }))
  assert.match(radio(html, 'type', 'multiplicative'), /disabled/)
  assert.ok(!/disabled/.test(radio(html, 'type', 'additive')))
  assert.ok(text(html).includes(REASON))
  // nothing wrong, nothing grey
  html = await render(Form, node({ ...SERIES, method: 'classical' }))
  assert.ok(!/disabled/.test(radio(html, 'type', 'multiplicative')) && !/disabled/.test(radio(html, 'transform', 'log')) && !text(html).includes(REASON))
  // STL has no such rule
  html = await render(Form, node({ ...SERIES, transform: 'log' }))
  assert.ok(!text(html).includes(REASON))
})

test('«Register the model as» is not drawn: the node writes no model', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node(SERIES, { mlflow: { connectionId: 3, experiment: 'sales' } }))
  const page = text(html)
  assert.ok(page.includes('Experiment') && page.includes('Continue without MLflow'))
  assert.ok(!page.includes('Register the model as'), page)
  assert.ok(page.includes('This node writes no model'), page)
})

test('the form is usable with the keyboard alone: native controls, each named, none that needs F, Home, End or Menu', () => {
  const form = read('../components/ui/ModelForm.vue')
  // the periods: a text box with a label, described by what was read and by the reason
  const numbers = form.slice(form.indexOf("f.control === 'numbers'"), form.indexOf("f.control === 'number_or_choice'"))
  assert.match(numbers, /<label :for="idOf\(f\.key\)"/)
  assert.match(numbers, /requiredNote\(f\) \? idOf\(f\.key, 'required'\)/)
  assert.match(numbers, /role="alert"/)
  assert.doesNotMatch(numbers, /@keydown|@keyup|tabindex/)
  // the choice between words and a value: a group of native radios with a legend and a named number box
  const either = form.slice(form.indexOf("f.control === 'number_or_choice'"), form.indexOf("f.control === 'choice' && asButtons(f)"))
  assert.ok(either.includes('<fieldset') && either.includes('<legend') && either.includes('type="radio"'))
  assert.match(either, /:aria-label="`\$\{labelOf\(f\)\}: \$\{\$t\('models\.form\.aValue'\)\}`"/)
  assert.doesNotMatch(either, /@keydown|@keyup|tabindex/)
  // the grey choices are disabled radios with the reason beside each in text
  assert.match(form, /:disabled="!!forbidden\(f, c\) && chosen\(f\) !== c"/)
  assert.match(form, /<span v-if="forbidden\(f, c\)" class="why"/)
})

// ── the palette, the canvas ──────────────────────────────────────────────────

test('the decomposition has its icon and its summary, and stays out of the palette until its runner is built', () => {
  assert.notEqual(icons.modelIconOf('decomposition'), icons.modelIconOf('arima'))
  assert.notEqual(icons.modelIconOf('decomposition'), icons.modelIconOf('linear'))
  const t = (k) => WORDS.en[k] ?? k
  const shown = m.MODEL_CATALOG.groups.filter((g) => g.id === 'timeseries')
  // today: only ARIMA and its six presets
  const now = m.modelPalette(shown, '', t)
  assert.ok(!now.flatMap((g) => g.items).some((i) => i.model === 'decomposition'))
  assert.ok(m.MODEL_NOT_IN_THE_PALETTE_YET.has('decomposition'))
  // the day it is let in: under Time series, one entry, after ARIMA's, named «Decomposition», and found by its name
  const open = m.modelPalette(shown, '', t, m.MODEL_CATALOG, new Set())
  assert.deepEqual(open.map((g) => g.group), ['timeseries'])
  const entries = open[0].items.filter((i) => i.model === 'decomposition')
  assert.equal(entries.length, 1)
  assert.equal(entries[0].preset, undefined)
  assert.equal(entries[0].kind, 'model:decomposition')
  assert.equal(t(entries[0].nameKey), 'Decomposition')
  assert.equal(open[0].items.at(-1).model, 'decomposition')
  assert.equal(m.modelPalette(shown, 'decomp', t, m.MODEL_CATALOG, new Set())[0].items.length, 1)
  // the canvas: the method over the series
  assert.equal(m.modelSummary('decomposition', { ...SERIES }), 'STL · orders')
  assert.equal(m.modelSummary('decomposition', { ...SERIES, period: 7 }), 'STL (7) · orders')
  assert.equal(m.modelSummary('decomposition', { ...SERIES, method: 'mstl', periods: [7, 365] }), 'MSTL (7, 365) · orders')
  assert.equal(m.modelSummary('decomposition', { ...SERIES, method: 'emd' }), 'EMD · orders')
  assert.equal(m.modelSummary('decomposition', { ...SERIES, method: 'classical', type: 'multiplicative', period: 12 }), 'classical × (12) · orders')
  assert.equal(m.modelSummary('decomposition', {}), 'STL')
})

// ── the columns before the Run ───────────────────────────────────────────────

test('the columns the node hands on are known before the Run, as the form edits them, and the steps after it keep them', () => {
  const names = (params, input = COLUMNS) => m.modelOutputColumns(DEC, params, input).columns.map((c) => c.name)
  let p = m.modelDefaults('decomposition')
  p = m.modelSet(DEC, p, 'time', 'day')
  p = m.modelSet(DEC, p, 'value', 'orders')
  // the frequency not chosen yet: no season, so no seasonal column
  assert.deepEqual(names(p), ['time', 'observed', 'filled', 'trend', 'remainder', 'deseasonalized'])
  p = m.modelSet(DEC, p, 'frequency', 'week')
  assert.deepEqual(names(p), ['time', 'observed', 'filled', 'trend', 'seasonal_52', 'remainder', 'deseasonalized'])
  p = m.modelSet(DEC, p, 'period', 7)
  assert.ok(names(p).includes('seasonal_7') && !names(p).includes('seasonal_52'))
  // MSTL: one per period, shortest first; the periods the box holds
  p = m.modelSet(DEC, p, 'method', 'mstl')
  p = m.modelSet(DEC, p, 'periods', [365, 7])
  assert.deepEqual(names(p).filter((n) => n.startsWith('seasonal_')), ['seasonal_7', 'seasonal_365'])
  // EMD: the IMFs asked for, the residue after
  p = m.modelSet(DEC, p, 'method', 'emd')
  assert.deepEqual(names(p).filter((n) => n.startsWith('imf_')), Array.from({ length: 10 }, (_, i) => `imf_${i + 1}`))
  p = m.modelSet(DEC, p, 'max_imf', 4)
  assert.deepEqual(names(p), ['time', 'observed', 'filled', 'imf_1', 'imf_2', 'imf_3', 'imf_4', 'residue'])
  // a transform adds the transformed series
  p = m.modelSet(DEC, p, 'transform', 'log')
  assert.ok(names(p).includes('transformed'))
  // the time is the input column's own type, and the series' columns are not the input's (the node hands on its own table)
  const out = m.modelOutputColumns(DEC, p, COLUMNS)
  assert.equal(out.columns[0].dtype, 'Date')
  assert.ok(!out.columns.some((c) => c.name === 'shop'))
  // the steps after it: a select of the columns, a rename, a drop, a filter
  const seasonal = m.modelSet(DEC, m.modelSet(DEC, m.modelDefaults('decomposition'), 'time', 'day'), 'frequency', 'day')
  const first = m.modelOutputColumns(DEC, { ...seasonal, value: 'orders' }, COLUMNS).columns
  assert.ok(first.some((c) => c.name === 'seasonal_7'))
  const kept = m.modelColumnsAfterStep('select', { columns: ['time', 'seasonal_7', 'gone'] }, first)
  assert.deepEqual(kept.map((c) => c.name), ['time', 'seasonal_7'])
  assert.deepEqual(m.modelColumnsAfterStep('rename', { mapping: { seasonal_7: 'weekly' } }, first).map((c) => c.name).filter((n) => /^(weekly|seasonal_)/.test(n)), ['weekly'])
  assert.ok(!m.modelColumnsAfterStep('drop', { columns: ['seasonal_7'] }, first).some((c) => c.name === 'seasonal_7'))
  // the results output: the table of numbers, not the series
  assert.ok(!names({ ...p, output: 'results' }).includes('trend'))
  // the flow editor asks for them with no run, in the panel and in the table's header
  const editor = read('../components/FlowEditor.vue')
  assert.match(editor, /modelOutputColumns\(def, model\.data\?\.params, input\)/)
  assert.match(editor, /def = model\?\.type === MODEL_NODE_TYPE && !scriptSources\.value\[model\.id\] \? modelOf\(model\.data\?\.model\) : null/)
})

// ── the words ────────────────────────────────────────────────────────────────

test('every word of the decomposition\'s form is in the five languages, with the same holes, and nothing is left in English', () => {
  // every key the node alone asks for: its name and hint, each field, choice and reason
  const wanted = m.modelWordKeys({ ...m.MODEL_CATALOG, models: [DEC] }).filter((k) => !k.startsWith('predict.'))
  assert.ok(wanted.length >= 50 && wanted.includes('models.params.periods') && wanted.includes('models.reasons.period_needed'))
  const holes = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((x) => x[1]).sort().join()
  for (const lang of LANGS) {
    for (const key of [...wanted, 'models.form.periodsPlaceholder']) {
      assert.ok(WORDS[lang][key]?.trim(), `${lang} ${key}`)
      assert.equal(holes(WORDS[lang][key]), holes(WORDS.en[key]), `${lang} ${key}`)
    }
    // the hint of every key the form draws (the shared words are the model's own where there are some)
    for (const f of m.modelFields(DEC)) {
      const hint = WORDS[lang][`models.decomposition.hints.${f.key}`] ?? WORDS[lang][`models.hints.${f.key}`]
      // (the shared keys time, value, alpha… have the words every model has, or none: a hint is optional)
      if (!['method', 'period', 'periods', 'type', 'robust', 's_window', 's_windows', 't_window', 'iterate', 'max_imf', 's_number', 'max_siftings', 'noise_imfs', 'missing', 'seasonality_test', 'lb_lag'].includes(f.key)) continue
      assert.ok(hint?.trim(), `${lang} decomposition.${f.key} has no hint`)
    }
  }
  // the sentences are in the language, not the engine's English
  for (const lang of LANGS.slice(1)) {
    for (const key of ['models.decomposition.hint', 'models.hints.method', 'models.hints.period', 'models.hints.periods', 'models.hints.lb_lag', 'models.reasons.multiplicative_transform',
      'models.reasons.period_needed', 'models.reasons.periods_needed', 'models.params.s_window', 'models.params.noise_imfs', 'models.form.periodsPlaceholder']) {
      assert.notEqual(WORDS[lang][key], WORDS.en[key], `${lang} ${key}`)
    }
  }
})

test('the form is drawn in every language with no word missing', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  for (const lang of LANGS) {
    const page = text(await render(Form, node({ ...SERIES, method: 'classical', type: 'multiplicative' }), lang))
    assert.ok(page.includes(WORDS[lang]['models.decomposition.hint']), lang)
    assert.ok(page.includes(WORDS[lang]['models.reasons.multiplicative_transform'].replace(/'/g, "'")), `${lang}: the reason`)
    assert.ok(page.includes(WORDS[lang]['models.params.type']), lang)
    const mstl = text(await render(Form, node({ ...SERIES, method: 'mstl' }), lang))
    assert.ok(mstl.includes(WORDS[lang]['models.params.periods']) && mstl.includes(WORDS[lang]['models.reasons.periods_needed']), lang)
    for (const html of [page, mstl, text(await render(Form, node({ ...SERIES, method: 'emd', transform: 'box_cox' }), lang)), text(await render(Form, node(SERIES), lang))]) {
      assert.ok(!/models\.[a-z_]+\.[\w.]+/.test(html), `${lang}: a key shown for a word: ${html.match(/models\.[\w.]+/)}`)
    }
  }
})
