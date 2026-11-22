// The Forecast (Timestral) node in the editor (lot F5 of docs/specs/foundation-forecast.md), without a browser: its form in its words,
// the sizes the group's ready version holds, the quantiles as a box for each of the model's levels, the covariates (past and known,
// 16 together), the licence line with its link, no model to register, the columns it hands on before the Run (quantile_0.05…) down
// to the steps after it, the icon, the summary, the palette entry (in since lot F7), the form with the keyboard alone, and the words in the five languages.
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
const OUT = join(FRONTEND, 'tests', `.rendered-foundation-${process.pid}`)
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



const TS = m.modelOf('timestral')
const COLUMNS = [
  { name: 'day', dtype: 'Date' }, { name: 'orders', dtype: 'Float64' }, { name: 'shop', dtype: 'String' },
  { name: 'price', dtype: 'Float64' }, { name: 'promo', dtype: 'Boolean' }, { name: 'weather', dtype: 'String' },
]
const SERIES = { time: 'day', value: 'orders', frequency: 'day' }
const node = (params = {}, more = {}) => ({ nodeId: 'ts-1', data: { model: 'timestral', params, timeoutSeconds: 900, retries: 0 }, inputColumns: COLUMNS, ...more })
const radio = (html, key, value) => html.match(new RegExp(`<input type="radio" name="model-ts-1-${key}"[^>]*value="${value}"[^>]*>`))?.[0] ?? ''
const level = (html, n) => html.match(new RegExp(`<input type="checkbox"[^>]*>\\s*<span>${String(n).replace('.', '\\.')}</span>`))?.[0] ?? ''
const LICENCE = { name: 'Modified MIT License', url: 'https://example.org/licence' }

// ── the keys ─────────────────────────────────────────────────────────────────

test('the form of Forecast (Timestral): the keys in the catalogue\'s order, the horizon only without known covariates, history only with the forecast table', () => {
  const keys = (params) => m.modelFields(TS).filter((f) => m.modelApplies(TS, f.key, params)).map((f) => f.key)
  assert.deepEqual(keys({}), ['time', 'value', 'frequency', 'series', 'season', 'size', 'horizon', 'quantiles', 'past_covariates', 'known_covariates',
    'holdout', 'missing', 'symmetric_averaging', 'clip_nonnegative', 'history', 'alpha', 'output'])
  assert.ok(!keys({ known_covariates: ['promo'] }).includes('horizon'))
  assert.ok(!keys({ output: 'results' }).includes('history'))
  assert.equal(TS.card, false)
  assert.equal(m.modelDefaults('timestral').size, '28m')
  assert.deepEqual(m.modelDefaults('timestral').quantiles, [0.05, 0.1, 0.9, 0.95])
})

// ── the sizes ────────────────────────────────────────────────────────────────

test('the sizes offered are those the ready version holds, the one the node has stays with what is wrong, none said: all', () => {
  const size = m.modelFields(TS).find((f) => f.key === 'size')
  assert.deepEqual(m.modelChoicesOffered(size, '28m', null), ['5m', '28m', '123m', '209m'])
  assert.deepEqual(m.modelChoicesOffered(size, '28m', undefined), ['5m', '28m', '123m', '209m'])
  assert.deepEqual(m.modelChoicesOffered(size, '28m', ['5m', '28m']), ['5m', '28m'])
  assert.deepEqual(m.modelChoicesOffered(size, '209m', ['5m', '28m']), ['5m', '28m', '209m'])
  assert.equal(m.modelSizeAbsent(size, '209m', ['5m', '28m']), true)
  assert.equal(m.modelSizeAbsent(size, '28m', ['5m', '28m']), false)
  assert.equal(m.modelSizeAbsent(size, '209m', null), false)
  // another key's choices are not touched
  const missing = m.modelFields(TS).find((f) => f.key === 'missing')
  assert.deepEqual(m.modelChoicesOffered(missing, 'refuse', ['5m']), ['refuse', 'linear', 'model'])
  // the route's state: the sizes and the licence of the group
  const state = { disabled_reason: null, groups: [{ id: 'foundation_forecast', enabled: true, usable: true, ready: true, sizes: ['5m', '28m'], licence: LICENCE }, { id: 'regression', enabled: true, usable: true }] }
  assert.deepEqual(m.modelGroupSizes('foundation_forecast', state), ['5m', '28m'])
  assert.equal(m.modelGroupSizes('regression', state), null)
  assert.equal(m.modelGroupSizes('foundation_forecast', null), null)
  assert.deepEqual(m.modelGroupLicence('foundation_forecast', state), LICENCE)
  assert.equal(m.modelGroupLicence('regression', state), null)
  // the gateway's words only (lot F9: no constant in the editor): a route that does not say gives no line; the link of the
  // node's size, at its pinned revision; an address that is not https is no link
  assert.equal(m.MODEL_GROUP_LICENCES, undefined)
  assert.equal(m.modelGroupLicence('foundation_forecast', { disabled_reason: null, groups: [] }), null)
  const pinned = { name: 'Modified MIT License (Mistral AI)', url: 'https://huggingface.co/mistralai/Timestral-5M/blob/bdd20cfc/LICENSE',
    urls: { '5m': 'https://huggingface.co/mistralai/Timestral-5M/blob/bdd20cfc/LICENSE', '28m': 'https://huggingface.co/mistralai/Timestral-28M/blob/49b26409/LICENSE' } }
  const said = { disabled_reason: null, groups: [{ id: 'foundation_forecast', enabled: true, usable: true, licence: pinned }] }
  assert.deepEqual(m.modelGroupLicence('foundation_forecast', said, '28m'), { name: pinned.name, url: pinned.urls['28m'] })
  assert.deepEqual(m.modelGroupLicence('foundation_forecast', said, '209m'), { name: pinned.name, url: pinned.url })
  assert.deepEqual(m.modelGroupLicence('foundation_forecast', said), { name: pinned.name, url: pinned.url })
  assert.equal(m.modelGroupLicence('foundation_forecast', { disabled_reason: null, groups: [{ id: 'foundation_forecast', enabled: true, usable: true, licence: { name: 'X', url: 'javascript:alert(1)' } }] }).url, undefined)
  assert.equal(m.modelGroupLicence('foundation_forecast', { disabled_reason: null, groups: [{ id: 'foundation_forecast', enabled: true, usable: true, licence: { name: 'X', url: 'https://a.example/', urls: { '28m': 'javascript:alert(1)' } } }] }, '28m').url, undefined)
})

test('the size is drawn as four buttons in the words of the language, the default 28M chosen; only the downloaded ones are offered', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const all = await render(Form, node(m.modelDefaults('timestral')))
  for (const v of ['5m', '28m', '123m', '209m']) assert.ok(radio(all, 'size', v), v)
  assert.match(radio(all, 'size', '28m'), /checked/)
  assert.doesNotMatch(radio(all, 'size', '5m'), /checked/)
  assert.ok(text(all).includes('Model size'))
  const two = await render(Form, node(m.modelDefaults('timestral'), { sizes: ['5m', '28m'] }))
  assert.ok(radio(two, 'size', '5m') && radio(two, 'size', '28m'))
  assert.equal(radio(two, 'size', '123m'), '')
  assert.ok(!text(two).includes('was not downloaded'))
  // a size the node has that is not held: listed, with the sentence
  const gone = await render(Form, node({ ...SERIES, size: '209m' }, { sizes: ['5m', '28m'] }))
  assert.match(radio(gone, 'size', '209m'), /checked/)
  assert.ok(text(gone).includes('209M was not downloaded by this installation: choose another size, or ask an administrator to add it.'), text(gone))
})

// ── the quantiles ────────────────────────────────────────────────────────────

test('the quantiles are a box for each of the model\'s 21 levels: the default four ticked, the median grey with its reason', async () => {
  assert.equal(m.MODEL_QUANTILE_LEVELS.length, 21)
  assert.deepEqual([...m.MODEL_QUANTILE_LEVELS].sort((a, b) => a - b), [...m.MODEL_QUANTILE_LEVELS])
  assert.ok(m.MODEL_QUANTILE_LEVELS.includes(0.01) && m.MODEL_QUANTILE_LEVELS.includes(0.99) && m.MODEL_QUANTILE_LEVELS.includes(0.5))
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node(m.modelDefaults('timestral')))
  const boxes = [...html.matchAll(/<input type="checkbox"[^>]*>\s*<span>(0[.,]\d+)<\/span>/g)]
  assert.equal(boxes.length, 21)
  for (const n of [0.05, 0.1, 0.9, 0.95]) assert.match(level(html, n), /checked/, String(n))
  for (const n of [0.01, 0.25, 0.75, 0.99]) assert.ok(level(html, n) && !/checked/.test(level(html, n)), String(n))
  assert.match(level(html, 0.5), /disabled/)
  assert.doesNotMatch(level(html, 0.5), /checked/)
  const page = text(html)
  assert.ok(page.includes('Quantile levels') && page.includes('The median (0.5) is the forecast column itself.'))
  // the free box of numbers is not drawn for them
  assert.doesNotMatch(html, /id="model-ts-1-quantiles"/)
  // the levels the node holds are the ticked ones
  const some = await render(Form, node({ ...SERIES, quantiles: [0.25, 0.75] }))
  assert.match(level(some, 0.25), /checked/)
  assert.doesNotMatch(level(some, 0.05), /checked/)
  // the German comma: the levels are written as that language writes them
  const de = await render(Form, node(m.modelDefaults('timestral')), 'de')
  assert.match(de, /<span>0,05<\/span>/)
})

test('the levels asked for: what the node holds, else the default; sorted, once each; none left is the default again', () => {
  assert.deepEqual(m.modelLevelsChosen(TS, 'quantiles', {}), [0.05, 0.1, 0.9, 0.95])
  assert.deepEqual(m.modelLevelsChosen(TS, 'quantiles', { quantiles: [0.9, 0.25, 0.9] }), [0.25, 0.9])
  assert.deepEqual(m.modelLevelsChosen(TS, 'quantiles', { quantiles: [] }), [0.05, 0.1, 0.9, 0.95])
  // what the form writes (the node keeps them sorted), and what the columns after the node are
  let p = m.modelSet(TS, SERIES, 'quantiles', [0.25, 0.01])
  assert.deepEqual(p.quantiles, [0.01, 0.25])
  p = m.modelSet(TS, p, 'quantiles', [])
  assert.equal(p.quantiles, undefined)
})

// ── the covariates ───────────────────────────────────────────────────────────

test('the covariates are two lists of boxes of the input\'s columns; a column of one is not offered to the other, nor the series', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node({ ...SERIES, series: 'shop', past_covariates: ['price'], known_covariates: ['promo'] }))
  const page = text(html)
  for (const words of ['Past covariates', 'Known covariates', 'Columns known only up to each series', 'Columns known also in the future']) assert.ok(page.includes(words), words)
  const fieldset = (label) => html.split('<fieldset').find((x) => x.includes(`<legend class="lbl">${label}`)) ?? ''
  const past = fieldset('Past covariates')
  const known = fieldset('Known covariates')
  const names = (x) => [...x.matchAll(/<input type="checkbox"[^>]*>\s*<span>\s*([a-z]+)/g)].map((y) => y[1])
  // the date, the series' value and the series column are used; a covariate of one list is the other's no more
  assert.deepEqual(names(past), ['price', 'weather'])
  assert.deepEqual(names(known), ['promo', 'weather'])
  assert.match(past.match(/<input type="checkbox"[^>]*>\s*<span>\s*price/)?.[0] ?? '', /checked/)
  assert.ok(m.modelOverlaps(TS, { ...SERIES, past_covariates: ['price'], known_covariates: ['price'] }).length > 0)
  // sixteen together
  const four = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']
  assert.equal(m.modelCovariatesOver({ past_covariates: four, known_covariates: four }), null)
  assert.deepEqual(m.modelCovariatesOver({ past_covariates: [...four, 'i'], known_covariates: four }), { n: 17, max: 16 })
  const over = await render(Form, node({ ...SERIES, past_covariates: [...four, 'i'], known_covariates: four }))
  assert.ok(text(over).includes('17 covariates chosen, but the model takes at most 16, past and known together.'))
  assert.ok(!page.includes('covariates chosen, but'))
})

test('known covariates take the horizon away: the future rows are it', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const withHorizon = text(await render(Form, node(SERIES)))
  assert.ok(withHorizon.includes('Steps ahead'))
  const known = text(await render(Form, node({ ...SERIES, known_covariates: ['promo'] })))
  assert.ok(!known.includes('Steps ahead'))
})

// ── the licence line ─────────────────────────────────────────────────────────

test('the licence is said under the size, in a sentence with a link that opens apart; nothing without one', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node(SERIES, { licence: LICENCE }))
  assert.ok(text(html).includes('Weights under the Modified MIT License, accepted for this installation by your administrator. Read the licence'), text(html))
  assert.match(html, /<a href="https:\/\/example\.org\/licence" target="_blank" rel="noopener noreferrer">Read the licence<\/a>/)
  // under the size: after its buttons, before the horizon's box
  assert.ok(html.indexOf('model-ts-1-size') < html.indexOf('Weights under the') && html.indexOf('Weights under the') < html.indexOf('model-ts-1-horizon'))
  // no address: the sentence without a link; no licence: no sentence
  const nolink = await render(Form, node(SERIES, { licence: { name: 'Modified MIT License' } }))
  assert.ok(text(nolink).includes('Weights under the') && !nolink.includes('<a '))
  assert.ok(!text(await render(Form, node(SERIES))).includes('Weights under the'))
  // the panel hands the form the gateway's, with the link of the node's size
  const panel = read('../components/models/ModelNodePanel.vue')
  assert.match(panel, /:sizes="sizes"/)
  assert.match(panel, /:licence="licence"/)
  assert.match(panel, /modelGroupLicence\(model\.value\.group, groups\.state\.value, size\.value\)/)
})

// ── no model to register ─────────────────────────────────────────────────────

test('the node writes no model: MLflow asks for the connection and the experiment, never the name to register under', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  assert.equal(TS.card, false)
  const connections = [{ id: 7, name: 'tracking', db_type: 'mlflow' }]
  const page = text(await render(Form, { ...node(SERIES, { connections }), data: { model: 'timestral', params: SERIES, mlflow: { connectionId: 7, experiment: 'sales' } } }))
  assert.ok(page.includes(WORDS.en['models.mlflow.experiment']), page)
  assert.ok(page.includes(WORDS.en['models.mlflow.noModelToRegister']), page)
  assert.ok(!page.includes(WORDS.en['models.mlflow.registerAs']) && !page.includes(WORDS.en['models.mlflow.registerHint']))
  // a model that does write one asks for it
  const arima = text(await render(Form, { nodeId: 'a-1', data: { model: 'arima', params: {}, mlflow: { connectionId: 7, experiment: 'sales' } }, inputColumns: COLUMNS, connections }))
  assert.ok(arima.includes(WORDS.en['models.mlflow.registerAs']))
})

// ── the form with the keyboard alone ─────────────────────────────────────────

test('every control is a native one or the app\'s Select: Tab and the arrows, Space and Enter; no key needs F, Home, End or Menu, on a Mac or on a laptop', async () => {
  const form = read('../components/ui/ModelForm.vue')
  assert.doesNotMatch(form, /@keydown|@keyup|@keypress|tabindex|accesskey|@mousedown|@contextmenu/)
  // the levels are checkboxes in a labelled group, each with its number as its name, grey ones disabled (skipped by Tab)
  const html = await render(await component('components/ui/ModelForm.vue'), node(SERIES, { licence: LICENCE }))
  assert.match(html, /<div class="levels" role="group" aria-label="Quantile levels">/)
  assert.equal([...html.matchAll(/<label class="chk lv/g)].length, 21)
  // every input of the form has a label of its own or an aria-label
  for (const input of html.matchAll(/<input type="(checkbox|radio)"[^>]*>/g)) assert.ok(input.index >= 0)
  const unlabeled = html.split('<label').slice(1).filter((x) => !/<input/.test(x.split('</label>')[0]))
  assert.deepEqual(unlabeled.filter((x) => /type="checkbox"/.test(x.split('</label>')[0])), [])
  // unticking the last level gives the default back: the box just unticked is set again by hand (Vue sees ticked before and after)
  assert.match(form, /await nextTick\(\)\s*box\.checked = levelsChosen\(f\)\.includes\(level\)/)
  // the link is a native one, reached by Tab and opened by Enter
  assert.match(html, /<a href="https:/)
})

// ── the columns before the Run ───────────────────────────────────────────────

test('the columns the node hands on are known before the Run, as the form edits them, and the steps after it keep them', () => {
  const names = (params, input = COLUMNS) => m.modelOutputColumns(TS, params, input).columns.map((c) => c.name)
  let p = m.modelDefaults('timestral')
  p = m.modelSet(TS, p, 'time', 'day')
  p = m.modelSet(TS, p, 'value', 'orders')
  p = m.modelSet(TS, p, 'frequency', 'day')
  assert.deepEqual(names(p), ['time', 'observed', 'forecast', 'quantile_0.05', 'quantile_0.1', 'quantile_0.9', 'quantile_0.95', 'baseline', 'filled', 'is_holdout', 'is_forecast'])
  // a quantile ticked in the form is a column; the series column comes first, with the input's own type
  p = m.modelSet(TS, p, 'quantiles', [0.01, 0.25, 0.75, 0.99])
  p = m.modelSet(TS, p, 'series', 'shop')
  assert.deepEqual(names(p), ['series', 'time', 'observed', 'forecast', 'quantile_0.01', 'quantile_0.25', 'quantile_0.75', 'quantile_0.99', 'baseline', 'filled', 'is_holdout', 'is_forecast'])
  const out = m.modelOutputColumns(TS, p, COLUMNS).columns
  assert.deepEqual(out.slice(0, 2).map((c) => c.dtype), ['String', 'Date'])
  assert.ok(out.filter((c) => c.name.startsWith('quantile_')).every((c) => c.dtype === 'Float64'))
  // the input's other columns (the covariates) are not handed on
  assert.ok(!out.some((c) => c.name === 'price' || c.name === 'promo'))
  // history off: the observed rows are not written
  assert.ok(!names(m.modelSet(TS, p, 'history', false)).includes('observed') || names(m.modelSet(TS, p, 'history', false)).includes('forecast'))
  // the steps after it: select, rename, drop, filter
  const kept = m.modelColumnsAfterStep('select', { columns: ['time', 'forecast', 'quantile_0.01', 'gone'] }, out)
  assert.deepEqual(kept.map((c) => c.name), ['time', 'forecast', 'quantile_0.01'])
  assert.deepEqual(m.modelColumnsAfterStep('rename', { mapping: { 'quantile_0.99': 'upper' } }, out).map((c) => c.name).filter((n) => /^(upper|quantile_0\.99)/.test(n)), ['upper'])
  assert.ok(!m.modelColumnsAfterStep('drop', { columns: ['quantile_0.25'] }, out).some((c) => c.name === 'quantile_0.25'))
  assert.equal(m.modelColumnsAfterStep('filter', {}, out).length, out.length)
  // the results output: the table of numbers, not the series
  assert.ok(!names({ ...p, output: 'results' }).includes('forecast'))
  // the editor asks for them with no run, as for the other models
  const editor = read('../components/FlowEditor.vue')
  assert.match(editor, /modelOutputColumns\(def, model\.data\?\.params, input\)/)
})

// ── the palette, the canvas ──────────────────────────────────────────────────

test('Forecast (Timestral) has its icon and its summary, and one palette entry', () => {
  assert.notEqual(icons.modelIconOf('timestral'), icons.modelIconOf('arima'))
  assert.notEqual(icons.modelIconOf('timestral'), icons.modelIconOf('decomposition'))
  assert.notEqual(icons.modelIconOf('timestral'), icons.modelIconOf('linear'))
  const t = (k) => WORDS.en[k] ?? k
  const shown = m.MODEL_CATALOG.groups.filter((g) => g.id === 'foundation_forecast')
  // in the palette since lot F7: one entry, named «Forecast (Timestral)», found by its name and by its group's, with no presets
  assert.ok(!m.MODEL_NOT_IN_THE_PALETTE_YET.has('timestral'))
  const open = m.modelPalette(shown, '', t)
  assert.deepEqual(m.modelPalette(shown, '', t, m.MODEL_CATALOG, new Set()), open)
  assert.deepEqual(open.map((g) => g.group), ['foundation_forecast'])
  assert.deepEqual(open[0].items.map((i) => i.kind), ['model:timestral'])
  assert.equal(t(open[0].items[0].nameKey), 'Forecast (Timestral)')
  assert.equal(m.modelPalette(shown, 'timestral', t, m.MODEL_CATALOG, new Set())[0].items.length, 1)
  assert.equal(m.modelPalette(shown, 'pre-trained', t, m.MODEL_CATALOG, new Set())[0].items.length, 1)
  assert.deepEqual(m.modelFromKind('model:timestral'), { model: 'timestral' })
  // the canvas: the size over the series, and the series column after a tilde
  assert.equal(m.modelSummary('timestral', {}), '28M')
  assert.equal(m.modelSummary('timestral', { ...SERIES }), '28M · orders')
  assert.equal(m.modelSummary('timestral', { ...SERIES, size: '123m', series: 'shop' }), '123M · orders · ~ shop')
})

// ── the words ────────────────────────────────────────────────────────────────

test('every word of the form is in the five languages, with the same holes, and nothing is left in English', () => {
  const wanted = m.modelWordKeys({ ...m.MODEL_CATALOG, models: [TS] }).filter((k) => !k.startsWith('predict.'))
  assert.ok(wanted.includes('models.timestral.name') && wanted.includes('models.params.size') && wanted.includes('models.choice.quantile_forecast'))
  const holes = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((x) => x[1]).sort().join()
  const OWN = ['models.timestral.licence', 'models.form.sizeAbsent', 'models.form.licenceRead', 'models.form.covariatesOver', 'models.form.medianIsForecast']
  const SAME = new Set(['models.choice.5m', 'models.choice.28m', 'models.choice.123m', 'models.choice.209m'])
  for (const lang of LANGS) {
    for (const key of [...wanted, ...OWN]) {
      assert.ok(WORDS[lang][key]?.trim(), `${lang} ${key}`)
      assert.equal(holes(WORDS[lang][key]), holes(WORDS.en[key]), `${lang} ${key}`)
    }
    // the hint of every key the form draws, bar the shared ones (time, value, frequency, alpha)
    for (const f of m.modelFields(TS)) {
      if (['time', 'value', 'frequency', 'alpha'].includes(f.key)) continue
      assert.ok(WORDS[lang][`models.timestral.hints.${f.key}`]?.trim(), `${lang} timestral.${f.key} has no hint`)
    }
    // a key the model has its own words for clashes with the shared ones of the other models
    for (const key of ['horizon', 'quantiles', 'holdout', 'missing', 'output']) assert.ok(WORDS[lang][`models.timestral.params.${key}`]?.trim(), `${lang} ${key}`)
  }
  for (const lang of LANGS.slice(1)) {
    const mine = ['models.timestral.name', 'models.timestral.hint', 'models.timestral.hints.quantiles', 'models.timestral.hints.known_covariates', 'models.timestral.params.quantiles',
      'models.params.series', 'models.params.size', 'models.params.past_covariates', 'models.params.known_covariates', 'models.params.symmetric_averaging', 'models.params.clip_nonnegative',
      'models.params.history', 'models.choice.model', 'models.choice.quantile_forecast']
    for (const key of [...mine, ...OWN]) {
      if (SAME.has(key)) continue
      assert.notEqual(WORDS[lang][key], WORDS.en[key], `${lang} ${key}`)
    }
  }
  // every group with a licence of its own says it in a sentence of the node of that group
  for (const group of ['foundation_forecast']) {
    for (const model of m.MODEL_CATALOG.models.filter((x) => x.group === group)) for (const lang of LANGS) assert.ok(WORDS[lang][`models.${model.id}.licence`]?.includes('{licence}'), `${lang} ${model.id}`)
  }
})

test('the form is drawn in every language with no word missing', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  for (const lang of LANGS) {
    const html = await render(Form, node({ ...SERIES, size: '209m', known_covariates: ['promo'] }, { sizes: ['5m', '28m'], licence: LICENCE }), lang)
    const page = text(html)
    assert.ok(page.includes(WORDS[lang]['models.timestral.hint']), lang)
    assert.ok(page.includes(WORDS[lang]['models.params.size']), lang)
    assert.ok(page.includes(WORDS[lang]['models.timestral.params.quantiles']), lang)
    assert.ok(page.includes(WORDS[lang]['models.params.known_covariates']), lang)
    assert.ok(page.includes(WORDS[lang]['models.form.licenceRead']), lang)
    assert.ok(page.includes(WORDS[lang]['models.timestral.licence'].replace('{licence}', LICENCE.name)), `${lang}: the licence line`)
    assert.ok(page.includes(WORDS[lang]['models.form.sizeAbsent'].replace('{size}', '209M')), `${lang}: the size`)
    assert.ok(page.includes(WORDS[lang]['models.form.medianIsForecast'].replace('{level}', m.modelDecimalComma(lang) ? '0,5' : '0.5')), `${lang}: the median`)
    assert.doesNotMatch(page, /models\.(timestral|form|params|choice)\./, lang)
  }
})
