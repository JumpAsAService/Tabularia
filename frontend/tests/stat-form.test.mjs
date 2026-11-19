// The statistics node's form and the palette's group, rendered: the two components are
// compiled as the app compiles them and drawn to HTML on the server renderer, with the real
// catalogue and the real English words. No browser: what is checked is what is on the page
// for a given node (which fields, which labels, which options, what is said and what is
// disabled), not how it looks or how it answers a click.
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
const OUT = join(FRONTEND, 'tests', `.rendered-${process.pid}`)
mkdirSync(OUT, { recursive: true })
after(() => rmSync(OUT, { recursive: true, force: true }))

const en = { ...(await import('../i18n/locales/en.ts')).default, ...(await import('../i18n/locales/en.gen.ts')).default }

/** A component of the app, compiled (script and template together) and imported. */
async function component (path) {
  const source = readFileSync(join(FRONTEND, path), 'utf8')
  const { descriptor, errors } = parse(source, { filename: path })
  assert.deepEqual(errors, [])
  // the one module of the form that needs Nuxt (the API client): only its «overtaken» check is used
  const content = compileScript(descriptor, { id: path, inlineTemplate: true }).content
    .replace("import { isSuperseded } from '~/composables/usePreviewSlots'", 'const isSuperseded = () => false')
  const file = join(OUT, `${path.replace(/[^\w]/g, '_')}.ts`)
  writeFileSync(file, content)
  return (await import(pathToFileURL(file).href)).default
}

// the app's Select, as a list of what it offers: its value, its options, its ARIA names
const Select = defineComponent({
  name: 'Select',
  inheritAttrs: false,
  props: ['modelValue', 'options', 'placeholder', 'searchable'],
  setup: (props, { attrs }) => () => h('div', { class: 'select', 'data-value': String(props.modelValue ?? ''), ...attrs },
    (props.options ?? []).map((o) => h('span', { class: 'option', 'data-hint': o.hint }, o.label ?? String(o.value)))),
})

async function render (comp, props) {
  const app = createSSRApp({ render: () => h(comp, props) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en }, missingWarn: false, fallbackWarn: false }))
  app.component('Select', Select)
  const warnings = []
  app.config.warnHandler = (m) => warnings.push(m)
  app.config.errorHandler = (e) => { throw e }
  const html = await renderToString(app)
  assert.deepEqual(warnings, [])
  return html
}
const text = (html) => html.replace(/<!--.*?-->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').trim()
const count = (html, piece) => html.split(piece).length - 1

const COLUMNS = [{ name: 'amount', dtype: 'Float64' }, { name: 'channel', dtype: 'String' }, { name: 'qty', dtype: 'Int64' }, { name: 'day', dtype: 'Date' }]
const OP = 'stat_t_two_sample'

test('a new two-sample t test: its fields from the catalogue, with their words', async () => {
  const Form = await component('components/ui/StatTestForm.vue')
  const params = { var_equal: false, mu: 0, alternative: 'two_sided', alpha: 0.05 }
  const html = await render(Form, { nodeId: 'op-1', opType: OP, params, inputColumns: COLUMNS })
  const page = text(html)
  for (const words of ['Do two groups have the same mean?', 'Value column', 'Group column', 'Groups to compare', 'Equal variances',
    'Value under H0', 'Alternative hypothesis', 'Two-sided (≠)', 'Less (<)', 'Greater (>)', 'Significance level (α)']) {
    assert.ok(page.includes(words), `«${words}» is not on the page: ${page}`)
  }
  // in the catalogue's order
  const order = ['Value column', 'Group column', 'Groups to compare', 'Equal variances', 'Value under H0', 'Alternative hypothesis', 'Significance level']
  assert.deepEqual([...order].sort((a, b) => page.indexOf(a) - page.indexOf(b)), order)
  // the measure: numeric columns only; the groups: any column
  const selects = [...html.matchAll(/<div class="select"[^>]*>(.*?)<\/div>/g)].map((m) => text(m[1]))
  assert.deepEqual(selects, ['amount qty', 'amount channel qty day'])
  // the groups wait for their column
  assert.ok(page.includes('Choose «Group column» first.'))
  // a switch, two numbers with what the node has, three radio buttons with the default chosen
  assert.equal(count(html, 'role="switch"'), 1)
  assert.ok(!/role="switch"[^>]*checked/.test(html))
  assert.equal(count(html, 'type="number"'), 2)
  assert.ok(html.includes('value="0.05"') && html.includes('value="0"'))
  assert.equal(count(html, 'type="radio"'), 3)
  assert.match(html, /<input type="radio"[^>]*value="two_sided"[^>]*checked/)
  assert.ok(!/value="less"[^>]*checked/.test(html) && !html.includes('disabled'))
  // nothing of the report, nothing about samples
  assert.ok(!/detail|report|sample|all rows/i.test(page))
  // every field is named for a screen reader
  assert.equal(count(html, 'aria-labelledby="stat-op-1-value-label"'), 1)
  assert.ok(html.includes('<label for="stat-op-1-alpha"') && html.includes('id="stat-op-1-alpha"'))
  assert.ok(html.includes('<legend') && html.includes('<fieldset'))
})

test('a set node shows what it has; a wrong alpha and a column that is no number are said', async () => {
  const Form = await component('components/ui/StatTestForm.vue')
  const params = { value: 'channel', group: 'day', var_equal: true, mu: 2, alternative: 'less', alpha: 5 }
  const html = await render(Form, { nodeId: 'op-2', opType: OP, params, inputColumns: COLUMNS })
  const page = text(html)
  assert.match(html, /class="select" data-value="channel"/)
  assert.match(html, /<span class="option" data-hint="not a number">channel<\/span>/) // kept in the list, with what is wrong
  assert.match(html, /role="switch"[^>]*checked/)
  assert.match(html, /value="less"[^>]*checked/)
  assert.ok(page.includes('A number between 0 and 1, both excluded.'))
  assert.match(html, /id="stat-op-2-alpha"[^>]*aria-invalid="true"/)
  assert.ok(!page.includes('Choose «Group column» first.'))
})

test('without columns the form says so, or waits; an operation that is no test draws nothing', async () => {
  const Form = await component('components/ui/StatTestForm.vue')
  const none = await render(Form, { nodeId: 'op-3', opType: OP, params: {}, inputColumns: [] })
  assert.ok(text(none).includes(en.paramForm.noUpstreamColumns))
  const waiting = await render(Form, { nodeId: 'op-3', opType: OP, params: {}, inputColumns: [], columnsLoading: true })
  assert.ok(waiting.includes('aria-busy="true"') && !text(waiting).includes(en.paramForm.noUpstreamColumns))
  const onlyText = await render(Form, { nodeId: 'op-3', opType: OP, params: {}, inputColumns: [{ name: 'channel', dtype: 'String' }] })
  assert.ok(text(onlyText).includes('The step\'s input has no numeric column'))
  assert.equal(text(await render(Form, { nodeId: 'op-3', opType: 'pivot', params: {}, inputColumns: COLUMNS })), '')
})

test('the groups of the chosen column are asked at the node\'s input, once, and awaited in their place', async () => {
  const Form = await component('components/ui/StatTestForm.vue')
  const asked = []
  const fetchDistinct = (column) => { asked.push(column); return new Promise(() => {}) }
  const html = await render(Form, { nodeId: 'op-4', opType: OP, params: { value: 'amount', group: 'channel' }, inputColumns: COLUMNS, fetchDistinct })
  assert.deepEqual(asked, ['channel'])
  const groups = html.slice(html.indexOf('id="stat-op-4-groups-label"'), html.indexOf('role="switch"'))
  assert.ok(groups.includes('aria-busy="true"') && !groups.includes('class="select"'))
  assert.ok(!text(html).includes('Choose «Group column» first.'))
  // no column for the groups: nothing is asked
  asked.length = 0
  await render(Form, { nodeId: 'op-4', opType: OP, params: { value: 'amount' }, inputColumns: COLUMNS, fetchDistinct })
  assert.deepEqual(asked, [])
})

test('the palette: the Statistics group between Transforms and Control, closed, its tests by family', async () => {
  const Sidebar = await component('components/ui/OpSidebar.vue')
  const operations = ['cast', 'filter', 'foreach', 'pivot', OP]
  const html = await render(Sidebar, { operations })
  const page = text(html)
  const order = ['Sources', 'Transforms', 'Statistics', 'Control', 'Output']
  assert.deepEqual([...order].sort((a, b) => page.indexOf(a) - page.indexOf(b)), order)
  // the title is a button that says it is closed, and how many tests it holds
  assert.match(html, /<button type="button" class="group-title group-toggle"[^>]*aria-expanded="false"[^>]*aria-controls="palette-statistics"/)
  assert.match(html, /id="palette-statistics"[^>]*style="display:none;"/)
  assert.match(html, /class="group-count"[^>]*>1</)
  // inside: the family, then its test, draggable and reachable with Tab, with its hint
  const group = html.slice(html.indexOf('id="palette-statistics"'), html.indexOf('Control'))
  assert.ok(text('<' + group).includes('Means Two-sample t test'), text('<' + group))
  assert.match(group, /class="item"[^>]*draggable="true"[^>]*role="button"[^>]*tabindex="0"[^>]*title="Do two groups have the same mean\?/)
  // the transformations do not list the test, and the test is listed once
  assert.equal(count(page, 'Two-sample t test'), 1)
  const transforms = html.slice(html.indexOf('Transforms'), html.indexOf('group-toggle'))
  assert.ok(text(transforms).includes('Pivot') && !text(transforms).includes('t test'))
  // an engine without tests: no group at all
  assert.ok(!text(await render(Sidebar, { operations: ['cast', 'filter'] })).includes('Statistics'))
})

test('an ANOVA: the correction shows with pairwise t tests only, a post-hoc its variances exclude is disabled with why', async () => {
  const Form = await component('components/ui/StatTestForm.vue')
  const base = { value: 'amount', group: 'channel', var_equal: true, posthoc: 'none', p_adjust: 'holm', alpha: 0.05 }
  let page = text(await render(Form, { nodeId: 'op-9', opType: 'stat_anova', params: base, inputColumns: COLUMNS }))
  assert.ok(page.includes('Pairwise comparisons') && !page.includes('Correction for multiple comparisons'))
  // the test's own hint for a shared key
  assert.ok(page.includes('On: the classic ANOVA, with the variance within the groups pooled.'))
  let html = await render(Form, { nodeId: 'op-9', opType: 'stat_anova', params: { ...base, posthoc: 'pairwise_t' }, inputColumns: COLUMNS })
  page = text(html)
  assert.ok(page.includes('Correction for multiple comparisons') && page.includes('Šidák'))
  assert.match(html, /value="holm"[^>]*checked/)
  // classic ANOVA: Games–Howell disabled, with the reason; Welch's: Tukey disabled
  assert.match(html, /<input type="radio"[^>]*value="games_howell"[^>]*disabled/)
  assert.ok(page.includes('Games–Howell is for unequal variances: with equal ones use Tukey\'s HSD.'))
  html = await render(Form, { nodeId: 'op-9', opType: 'stat_anova', params: { ...base, var_equal: false }, inputColumns: COLUMNS })
  assert.match(html, /<input type="radio"[^>]*value="tukey"[^>]*disabled/)
  assert.ok(!/value="games_howell"[^>]*disabled/.test(html))
})

test('a paired test asks for two numeric columns; the F test of variances for a ratio above 0', async () => {
  const Form = await component('components/ui/StatTestForm.vue')
  let html = await render(Form, { nodeId: 'op-10', opType: 'stat_t_paired', params: { mu: 0, alternative: 'two_sided', alpha: 0.05 }, inputColumns: COLUMNS })
  const selects = [...html.matchAll(/<div class="select"[^>]*>(.*?)<\/div>/g)].map((m) => text(m[1]))
  assert.deepEqual(selects, ['amount qty', 'amount qty'])
  assert.ok(text(html).includes('First column (x)') && text(html).includes('the test is on x − y'))
  html = await render(Form, { nodeId: 'op-11', opType: 'stat_var_f', params: { value: 'amount', group: 'channel', ratio: 0 }, inputColumns: COLUMNS })
  assert.ok(text(html).includes('A number above 0.') && text(html).includes('Ratio under H0'))
})

test('a test of one proportion: the success is written or chosen among the column\'s values; the correction with the score only', async () => {
  const Form = await component('components/ui/StatTestForm.vue')
  const asked = []
  const fetchDistinct = (column) => { asked.push(column); return Promise.resolve(['yes', 'no', null]) }
  let html = await render(Form, { nodeId: 'op-20', opType: 'stat_prop_one_sample', params: { value: 'channel', success: 'yes', p0: 0.25 },
    inputColumns: COLUMNS, fetchDistinct })
  let page = text(html)
  assert.deepEqual(asked, ['channel'])
  for (const words of ['Outcome column', 'Value that counts as a success', 'Proportion under H0 (p0)', 'Score (Wilson)', 'Exact (binomial)',
    'Continuity correction']) assert.ok(page.includes(words), words)
  assert.match(html, /<input id="stat-op-20-success" type="text" autocomplete="off" list="stat-op-20-success-values" value="yes"/)
  assert.ok(html.includes('<datalist id="stat-op-20-success-values"'))
  // any column may be the outcome: the texts are offered too
  const selects = [...html.matchAll(/<div class="select"[^>]*>(.*?)<\/div>/g)].map((m) => text(m[1]))
  assert.deepEqual(selects, ['amount channel qty day'])
  html = await render(Form, { nodeId: 'op-20', opType: 'stat_prop_one_sample', params: { value: 'channel', success: 'yes', method: 'exact' },
    inputColumns: COLUMNS })
  assert.ok(!text(html).includes('Continuity correction'))
})

test('a goodness of fit: all equal until proportions are given, a box for each category, their sum said', async () => {
  const Form = await component('components/ui/StatTestForm.vue')
  let html = await render(Form, { nodeId: 'op-21', opType: 'stat_chi2_gof', params: { value: 'channel' }, inputColumns: COLUMNS })
  let page = text(html)
  assert.ok(page.includes('Expected proportions') && page.includes('All equal') && page.includes('Given for each category'))
  assert.match(html, /<input type="radio" name="stat-op-21-expected" checked/)
  assert.ok(!html.includes('stat-op-21-expected-share-0'))
  // proportions given (one not in the data any more): a box each, the sum and what is wrong
  html = await render(Form, { nodeId: 'op-21', opType: 'stat_chi2_gof', params: { value: 'channel', expected: { web: 0.5, shop: 0.6 } },
    inputColumns: COLUMNS })
  page = text(html)
  assert.ok(html.includes('id="stat-op-21-expected-share-0"') && html.includes('aria-label="Proportion of «web»"'))
  assert.ok(page.includes('The proportions sum to 1.1: they must sum to 1.'), page)
})
