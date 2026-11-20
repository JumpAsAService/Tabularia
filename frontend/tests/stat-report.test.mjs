// The Report tab of a statistics node (docs/specs/statistics.md, «In the editor»): what is
// asked of POST /tasks/stat-report, when the pages on screen are stale, the key they are
// remembered by, what a failure is called — and the view itself, compiled as the app
// compiles it and drawn on the server renderer with the real English words: what it asks
// when it opens, what it shows for a node not set yet, that nothing in it chooses the rows.
// Run: npm test
import { test, after } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createI18n } from 'vue-i18n'

register('./alias-hooks.mjs', import.meta.url)
const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const OUT = join(FRONTEND, 'tests', `.rendered-report-${process.pid}`)
mkdirSync(OUT, { recursive: true })
after(() => rmSync(OUT, { recursive: true, force: true }))

const R = await import('../composables/statReport.ts')
const { Recent, previewKey } = await import('../composables/previewGrid.ts')
const en = { ...(await import('../i18n/locales/en.ts')).default, ...(await import('../i18n/locales/en.gen.ts')).default }

const OP = 'stat_t_two_sample'
const TEST = { type: OP, params: { value: 'amount', group: 'channel', var_equal: false, mu: 0, alternative: 'two_sided', alpha: 0.05 } }
const SAMPLE = { type: 'limit', params: { n: 100000 }, _dev_sample: true }
const CHAIN = { engine: 'duckdb', bucket: 'data', input_key: 'ds/orders.parquet', operations: [SAMPLE, { type: 'filter', params: { column: 'amount', op: 'gt', value: 0 } }, TEST] }

// ── the request ──────────────────────────────────────────────────────────────
test('the request is the preview\'s chain as it is, with the format and the language', () => {
  const body = R.statReportRequest(CHAIN, 'svg', 'it', 'Welch on the channels')
  assert.deepEqual(Object.keys(body), ['engine', 'bucket', 'input_key', 'operations', 'format', 'locale', 'step'])
  assert.equal(body.operations, CHAIN.operations) // not copied, not changed
  assert.deepEqual(body.operations[0], SAMPLE) // the development sample's mark travels
  assert.ok(!('detail' in body.operations[2].params) && !('limit' in body) && !('filename' in body))
  assert.equal(body.format, 'svg')
  assert.equal(body.locale, 'it')
  assert.equal(body.step, 'Welch on the channels')
  // a language the report has not falls back on English; a regional tag is its language
  assert.equal(R.statReportRequest(CHAIN, 'pdf', 'pt', '').locale, 'en')
  assert.equal(R.statReportRequest(CHAIN, 'pdf', 'de-CH', '').locale, 'de')
  // no name: no key; a long one is cut at the route's limit
  assert.ok(!('step' in R.statReportRequest(CHAIN, 'xlsx', 'en', '  ')))
  assert.equal(R.statReportRequest(CHAIN, 'xlsx', 'en', 'x'.repeat(300)).step.length, R.STAT_REPORT_STEP_MAX)
})

test('the key: the preview\'s identity, the language and the step\'s name', () => {
  const key = R.statReportKey(CHAIN, 'en', 'A')
  assert.equal(key, R.statReportKey(JSON.parse(JSON.stringify(CHAIN)), 'en', 'A'))
  assert.ok(key.includes(JSON.stringify(previewKey({ ...CHAIN, limit: 0 }))))
  const other = (change) => R.statReportKey({ ...CHAIN, ...change }, 'en', 'A')
  assert.notEqual(key, other({ engine: 'polars' }))
  assert.notEqual(key, other({ input_key: 'ds/other.parquet' }))
  assert.notEqual(key, other({ operations: [CHAIN.operations[1], TEST] })) // the sample taken off the source
  assert.notEqual(key, other({ operations: [SAMPLE, CHAIN.operations[1], { ...TEST, params: { ...TEST.params, alpha: 0.01 } }] }))
  assert.notEqual(key, R.statReportKey(CHAIN, 'it', 'A'))
  assert.notEqual(key, R.statReportKey(CHAIN, 'en', 'B'))
})

test('a test that lacks a column it needs is not asked', () => {
  assert.equal(R.statReportUnset(CHAIN.operations), false)
  assert.equal(R.statReportUnset([{ type: OP, params: { value: 'amount', var_equal: false } }]), true)
  assert.equal(R.statReportUnset([{ type: OP, params: { value: 'amount', group: '' } }]), true)
  assert.equal(R.statReportUnset([{ type: OP, params: null }]), true)
  // `groups` is not needed (two groups are found by themselves): the engine judges it
  assert.equal(R.statReportUnset([{ type: OP, params: { value: 'amount', group: 'channel', groups: null } }]), false)
  assert.equal(R.statReportUnset([{ type: 'filter', params: {} }]), false)
  assert.equal(R.statReportUnset([]), false)
  assert.equal(R.statReportTest(CHAIN.operations)?.id, 't_two_sample')
  assert.equal(R.statReportTest([SAMPLE]), null)
})

// ── the states ───────────────────────────────────────────────────────────────
test('which state the tab is in', () => {
  const s = (o) => R.statReportState({ unset: false, busy: false, key: 'k2', shownKey: null, failedKey: null, ...o })
  assert.equal(s({}), 'empty')
  assert.equal(s({ busy: true }), 'drawing')
  assert.equal(s({ shownKey: 'k2' }), 'ready')
  assert.equal(s({ shownKey: 'k2', busy: true }), 'ready')
  assert.equal(s({ shownKey: 'k1' }), 'stale') // set differently since: the pages stay
  assert.equal(s({ shownKey: 'k1', busy: true }), 'stale')
  assert.equal(s({ shownKey: 'k1', unset: true }), 'stale')
  assert.equal(s({ unset: true }), 'unset')
  assert.equal(s({ failedKey: 'k2' }), 'failed')
})

test('what draws by itself: the tab opened, another node, another language — never a parameter', () => {
  const at = (node, locale = 'en') => ({ node, locale })
  assert.equal(R.statReportShouldDraw(at('op-1'), null), true)
  assert.equal(R.statReportShouldDraw(at('op-2'), at('op-1')), true)
  assert.equal(R.statReportShouldDraw(at('op-1', 'it'), at('op-1')), true)
  assert.equal(R.statReportShouldDraw(at('op-1'), at('op-1')), false)
  assert.equal(R.statReportShouldDraw(at(null), at('op-1')), false)
})

// ── zoom ─────────────────────────────────────────────────────────────────────
test('the zoom: 75% until chosen, kept, stepped', () => {
  assert.equal(R.statReportZoomRead(null), 0.75)
  assert.equal(R.statReportZoomRead(''), 0.75)
  assert.equal(R.statReportZoomRead('fit'), 'fit')
  assert.equal(R.statReportZoomRead('1.25'), 1.25)
  assert.equal(R.statReportZoomRead('nonsense'), 0.75)
  assert.equal(R.statReportZoomRead('99'), 4)
  assert.equal(R.statReportZoomRead('0.01'), 0.25)
  assert.equal(R.statReportZoomStep(0.75, 1), 0.9)
  assert.equal(R.statReportZoomStep(0.75, -1), 0.67)
  assert.equal(R.statReportZoomStep(0.8123, 1), 0.9) // from a «fit» scale
  assert.equal(R.statReportZoomStep(4, 1), null)
  assert.equal(R.statReportZoomStep(0.25, -1), null)
})

// ── failures ─────────────────────────────────────────────────────────────────
test('a failure by its status: a refusal is the test\'s sentence, the others have a name', () => {
  const kind = (statusCode, message = 'x') => R.statReportProblem({ statusCode }, message).kind
  assert.equal(kind(400, 'stat_t_two_sample: the column «channel» has 3 groups'), 'refusal')
  assert.equal(kind(400, "stat_t_two_sample: 'group' is required"), 'unset')
  assert.equal(kind(403), 'forbidden')
  assert.equal(kind(429), 'busy')
  assert.equal(kind(413), 'size')
  assert.equal(kind(504), 'timeout')
  assert.equal(kind(503), 'charts')
  assert.equal(kind(404), 'gone')
  assert.equal(kind(422), 'invalid')
  assert.equal(kind(500), 'other')
  assert.equal(kind(undefined), 'other')
  // the POST's own failure carries its status on the response
  assert.equal(R.statReportProblem({ response: { status: 403 } }, 'no').kind, 'forbidden')
  assert.equal(R.statReportProblem({ statusCode: 400 }, 'the sentence').message, 'the sentence')
  // asking again as it is helps only when the setup is not what is wrong
  assert.deepEqual(['refusal', 'unset', 'forbidden', 'invalid', 'busy', 'timeout', 'charts', 'gone', 'size', 'other'].map(R.statReportRetryable),
    [false, false, false, false, true, true, true, true, true, true])
  // every kind with a title has its words
  for (const k of ['forbidden', 'busy', 'size', 'timeout', 'charts', 'gone']) assert.ok(en.statReport.problems[k], k)
})

// ── the view ─────────────────────────────────────────────────────────────────
// a render that never ends (the bench answers nothing) keeps its seconds' clock: here it
// must not keep the process
const realInterval = globalThis.setInterval
globalThis.setInterval = (...args) => { const timer = realInterval(...args); timer.unref?.(); return timer }
const asked = []
globalThis.__statReportBench = {
  apiFetch: (url, opts) => { asked.push({ url, ...opts }); return new Promise(() => {}) },
}

/** StatReportView compiled; what Nuxt auto-imports is handed by the bench, the pages are a
 *  stand-in that says what it was given. */
async function view () {
  const path = 'components/ui/StatReportView.vue'
  const { descriptor, errors } = parse(readFileSync(join(FRONTEND, path), 'utf8'), { filename: path })
  assert.deepEqual(errors, [])
  const content = `import { h as __h } from 'vue'
const useApiClient = () => ({ apiFetch: globalThis.__statReportBench.apiFetch })
const useReports = () => ({ followRender: () => new Promise(() => {}), screenOf: () => new Promise(() => {}) })
const useToast = () => ({ error: () => {}, success: () => {} })
const ReportPages = { props: ['pages', 'zoom', 'busy', 'label'], setup: (p) => () => __h('div', { class: 'pages', 'data-pages': String(p.pages.length), 'data-zoom': String(p.zoom), 'data-busy': String(p.busy), 'aria-label': p.label }) }
` + compileScript(descriptor, { id: path, inlineTemplate: true }).content
    .replace("import ReportPages from '~/components/report/ReportPages.vue'", '')
    .replace("import { errMessage } from '~/composables/useApi'", 'const errMessage = (e) => String(e?.message ?? e)')
  const file = join(OUT, 'StatReportView.ts')
  writeFileSync(file, content)
  return (await import(pathToFileURL(file).href)).default
}

async function render (comp, props, locale = 'en') {
  const app = createSSRApp({ render: () => h(comp, props) })
  app.use(createI18n({ legacy: false, locale, messages: { en, it: en }, missingWarn: false, fallbackWarn: false }))
  const warnings = []
  app.config.warnHandler = (m) => warnings.push(m)
  app.config.errorHandler = (e) => { throw e }
  const html = await renderToString(app)
  assert.deepEqual(warnings, [])
  return html
}
const text = (html) => html.replace(/<!--.*?-->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()

test('the tab opened: one request, the chain untouched, the page-shaped wait; nothing chooses the rows', async () => {
  const View = await view()
  asked.length = 0
  const html = await render(View, { nodeId: 'op-1', chain: CHAIN, step: 'Two-sample t test amount ~ channel' }, 'it')
  assert.equal(asked.length, 1)
  assert.equal(asked[0].url, '/tasks/stat-report')
  assert.equal(asked[0].method, 'POST')
  assert.deepEqual(asked[0].body, { ...CHAIN, format: 'svg', locale: 'it', step: 'Two-sample t test amount ~ channel' })
  assert.match(html, /class="pages" data-pages="0" data-zoom="0.75" data-busy="true"/)
  const page = text(html)
  assert.ok(page.includes('Drawing the report…'))
  // zoom, fit, PDF, Excel, expand: each a button with its name; the downloads wait for pages
  for (const name of ['Zoom out', 'Zoom in', 'Fit width', 'Download these pages as a PDF', 'Download the tables of these pages as an Excel file', 'Expand the report']) {
    assert.ok(html.includes(`aria-label="${name}"`), name)
  }
  assert.equal((html.match(/<button[^>]*disabled[^>]*aria-label="Download/g) ?? []).length + (html.match(/<button[^>]*aria-label="Download[^>]*disabled/g) ?? []).length, 2)
  assert.match(html, /aria-pressed="false"[^>]*aria-label="Expand the report"|aria-label="Expand the report"[^>]*aria-pressed="false"/)
  // read-only, and on the rows that reach the node: no control about samples or rows
  assert.ok(!/sample|all rows|every row|whole/i.test(page), page)
  assert.ok(!/<input|<select|role="switch"|type="checkbox"/.test(html))
})

test('a test not set yet: the hint, and nothing asked; a node without data says so', async () => {
  const View = await view()
  asked.length = 0
  const unset = { ...CHAIN, operations: [{ type: OP, params: { var_equal: false } }] }
  const html = await render(View, { nodeId: 'op-2', chain: unset })
  assert.equal(asked.length, 0)
  assert.ok(text(html).includes(en.statReport.unset))
  assert.ok(!html.includes('class="pages"'))
  const none = await render(View, { nodeId: 'op-2', chain: null })
  assert.equal(asked.length, 0)
  assert.ok(text(none).includes(en.statReport.noData))
})

test('pages this page has drawn already are shown at once, with their downloads', async () => {
  const View = await view()
  asked.length = 0
  const memory = new Recent(4)
  const key = R.statReportKey(CHAIN, 'en', 'T')
  memory.set(key, { key, pages: ['<svg/>', '<svg/>'], chain: CHAIN, locale: 'en', step: 'T' })
  const html = await render(View, { nodeId: 'op-1', chain: CHAIN, step: 'T', memory, expanded: true })
  assert.equal(asked.length, 0)
  assert.match(html, /class="pages" data-pages="2" data-zoom="0.75" data-busy="false"/)
  assert.ok(text(html).includes('Page 1 of 2'))
  assert.ok(!/aria-label="Download[^>]*disabled|disabled[^>]*aria-label="Download/.test(html))
  assert.ok(!text(html).includes('Not up to date'))
  assert.match(html, /aria-pressed="true"/)
  // the same node in another language is another report: asked
  await render(View, { nodeId: 'op-1', chain: CHAIN, step: 'T', memory }, 'it')
  assert.equal(asked.length, 1)
  assert.equal(asked[0].body.locale, 'it')
})

test('the editor shows the tab for a statistics or a model node only, and falls back to Table', () => {
  const editor = readFileSync(join(FRONTEND, 'components/FlowEditor.vue'), 'utf8')
  assert.match(editor, /<button\s+v-if="reportNodeId"\s+role="tab"/)
  assert.match(editor, /const reportNodeId = computed\(\(\) => statNodeId\.value \?\? modelReportNodeId\.value\)/)
  assert.match(editor, /findNode\(id\)\?\.type === 'model' \? id : null/)
  // a model node's runs are those the outputs give Script nodes; its Run is theirs
  assert.match(editor, /statModelReportRun\(scriptOutputs\.value\[id\]\)/)
  assert.match(editor, /@run="modelReportNodeId && runScriptNode\(modelReportNodeId\)"/)
  assert.match(editor, /statIsOp\(n\.data\?\.opType\) \? id : null/)
  assert.match(editor, /if \(!id && viewTab\.value === 'report'\) viewTab\.value = 'table'/)
  // the chain is the preview's own, with the flow's engine: no second chain is built
  assert.match(editor, /previewBody\(statNodeId\.value\)\.body/)
  // a test's table is not its input reshaped: no «new» on its ten columns, no «N columns removed»
  assert.match(editor, /if \(statIsOp\(op\)\) return null\n\s+const renames = /)
})

// ── a model node (docs/specs/statistical-models.md, «The report») ─────────────
const RUNS = { flowId: 42, runId: 9811, stale: false, trial: true, failed: false, running: false }

test('a model node\'s report is its run\'s: the request names the run, the key the run and the language', () => {
  const target = { flowId: 42, nodeId: 'ctl-7', runId: 9811 }
  assert.deepEqual(R.statModelReportRequest(target, 'pdf', 'de-CH'), { flow_id: 42, node_id: 'ctl-7', run_id: 9811, format: 'pdf', locale: 'de' })
  assert.equal(R.statModelReportKey(target, 'en'), R.statModelReportKey({ ...target }, 'en'))
  assert.notEqual(R.statModelReportKey(target, 'en'), R.statModelReportKey({ ...target, runId: 9812 }, 'en'))
  assert.notEqual(R.statModelReportKey(target, 'en'), R.statModelReportKey(target, 'it'))
  // the run drawn: the latest when it succeeded, else the latest that did; none yet
  const ok = { run_id: 5, status: 'SUCCESS', trial: true, stale: true }
  assert.deepEqual(R.statModelReportRun({ last: ok, output: null }), { runId: 5, stale: true, trial: true, failed: false })
  assert.deepEqual(R.statModelReportRun({ last: { run_id: 6, status: 'FAILURE' }, output: { run_id: 5, trial: false, stale: false } }),
    { runId: 5, stale: false, trial: false, failed: true })
  assert.deepEqual(R.statModelReportRun({ last: { run_id: 6, status: 'FAILURE' }, output: null }), { runId: null, stale: false, trial: false, failed: true })
  assert.deepEqual(R.statModelReportRun(undefined), { runId: null, stale: false, trial: false, failed: false })
  assert.deepEqual(R.statModelReportRun({ last: { run_id: 7, status: 'STARTED' }, output: null }).runId, null)
})

test('a model node with a run: one request for its pages, nothing of the chain', async () => {
  const View = await view()
  asked.length = 0
  const html = await render(View, { nodeId: 'ctl-7', chain: CHAIN, model: RUNS }, 'it')
  assert.equal(asked.length, 1)
  assert.equal(asked[0].url, '/tasks/model-report')
  assert.deepEqual(asked[0].body, { flow_id: 42, node_id: 'ctl-7', run_id: 9811, format: 'svg', locale: 'it' })
  assert.ok(html.includes(`aria-label="${en.modelReport.region}"`))
})

test('a model node with no run yet says so and offers Run; one whose run failed says where the log is', async () => {
  const View = await view()
  asked.length = 0
  const none = await render(View, { nodeId: 'ctl-7', chain: null, model: { ...RUNS, runId: null } })
  assert.equal(asked.length, 0)
  assert.ok(text(none).includes(en.modelReport.noRun) && text(none).includes(en.modelReport.run))
  assert.ok(!text(none).includes(en.statReport.noData) && !none.includes('class="pages"'))
  const failed = await render(View, { nodeId: 'ctl-7', chain: null, model: { ...RUNS, runId: null, failed: true } })
  assert.ok(text(failed).replaceAll('&#39;', "'").includes(en.modelReport.failed))
  const running = await render(View, { nodeId: 'ctl-7', chain: null, model: { ...RUNS, runId: null, running: true } })
  assert.ok(text(running).includes(en.modelReport.running))
  assert.equal(asked.length, 0)
})

test('a model\'s pages of a run older than the node stay, marked, with Run', async () => {
  const View = await view()
  asked.length = 0
  const memory = new Recent(4)
  const key = R.statModelReportKey({ flowId: 42, nodeId: 'ctl-7', runId: 9811 }, 'en')
  memory.set(key, { key, pages: ['<svg/>'], chain: null, model: { flowId: 42, nodeId: 'ctl-7', runId: 9811 }, locale: 'en', step: '' })
  const html = await render(View, { nodeId: 'ctl-7', chain: null, model: { ...RUNS, stale: true }, memory })
  assert.equal(asked.length, 0)
  assert.match(html, /class="pages" data-pages="1"/)
  const page = text(html)
  assert.ok(page.includes(en.statReport.stale) && page.includes(en.modelReport.run) && page.includes(en.modelReport.trial))
  assert.ok(!page.includes(en.statReport.update))
  const fresh = await render(View, { nodeId: 'ctl-7', chain: null, model: RUNS, memory })
  assert.ok(!text(fresh).includes(en.statReport.stale))
})

// ── a render the tab left is stopped on the server ───────────────────────────
test('the path that stops a render names the task, encoded; the node settles before it is drawn', () => {
  assert.equal(R.statReportCancelPath('0123-abcd'), '/tasks/exports/0123-abcd/cancel')
  assert.equal(R.statReportCancelPath('a/b'), '/tasks/exports/a%2Fb/cancel')
  assert.ok(R.STAT_REPORT_SETTLE_MS >= 200 && R.STAT_REPORT_SETTLE_MS <= 600)
})

test('the view stops a render it abandons and waits for the node to settle', () => {
  const src = readFileSync(join(FRONTEND, 'components/ui/StatReportView.vue'), 'utf8')
  // abandoned in flight (superseded, left, unmounted) or while the request was on its way
  assert.match(src, /if \(started\) cancelRender\(started\)/)
  assert.match(src, /if \(controller !== mine\) \{ cancelRender\(task_id\); return \}/)
  assert.match(src, /apiFetch\(statReportCancelPath\(taskId\), \{ method: 'POST' \}\)/)
  // a change of node draws after the settle time, and a stop or unmount cancels the wait
  assert.match(src, /setTimeout\(\(\) => \{ settle = undefined; busy\.value = false; draw\(\) \}, STAT_REPORT_SETTLE_MS\)/)
  assert.equal((src.match(/clearTimeout\(settle\)/g) ?? []).length >= 3, true)
})
