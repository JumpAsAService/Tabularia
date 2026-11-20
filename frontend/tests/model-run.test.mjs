// «Run» of a model node without a browser (components/models/ModelRun.vue, docs/specs/statistical-models.md,
// «The trial: Run in the editor»): the button and what it says (save first when the flow has changes, the
// seconds while it runs), the latest trial or run with its rows, the runner's sentence of a refusal, its log,
// «not up to date» when the node or its chain changed; a node that cannot run yet says why. The panels hand
// the button what the editor knows (NodePanel → ModelNodePanel → ModelForm's slot), and the editor runs it
// as a Script node's trial.
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
const OUT = join(FRONTEND, 'tests', `.rendered-model-run-${process.pid}`)
mkdirSync(OUT, { recursive: true })
after(() => rmSync(OUT, { recursive: true, force: true }))

const read = (path) => readFileSync(join(FRONTEND, path), 'utf8')
const LANGS = ['en', 'it', 'de', 'es', 'fr']
const MESSAGES = {}
for (const lang of LANGS) {
  MESSAGES[lang] = { ...(await import(`../i18n/locales/${lang}.ts`)).default, ...(await import(`../i18n/locales/${lang}.gen.ts`)).default }
}

async function component (path) {
  const { descriptor, errors } = parse(read(path), { filename: path })
  assert.deepEqual(errors, [])
  const file = join(OUT, `${path.replace(/[^\w]/g, '_')}.ts`)
  writeFileSync(file, compileScript(descriptor, { id: path, inlineTemplate: true }).content)
  return (await import(pathToFileURL(file).href)).default
}

async function render (comp, props, locale = 'en') {
  const app = createSSRApp({ render: () => h(comp, props) })
  app.use(createI18n({ legacy: false, locale, messages: { [locale]: MESSAGES[locale] }, missingWarn: false, fallbackWarn: false }))
  const warnings = []
  app.config.warnHandler = (w) => warnings.push(w)
  app.config.errorHandler = (e) => { throw e }
  const html = await renderToString(app)
  assert.deepEqual(warnings, [])
  return html
}
const text = (html) => html.replace(/<!--.*?-->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').replace(/&#39;/g, "'").trim()
const button = (html) => html.match(/<button[^>]*>/)[0]

const Run = await component('components/models/ModelRun.vue')
const hourAgo = new Date(Date.now() - 3600_000).toISOString()
const last = (more = {}) => ({ run_id: 7, status: 'SUCCESS', trial: true, error: null, started_at: hourAgo, finished_at: hourAgo,
  attempts: 1, exit_code: 0, log: '', stale: false, ...more })

test('a node never run: the button and what it does', async () => {
  const html = await render(Run, {})
  assert.ok(!button(html).includes('disabled'))
  const page = text(html)
  assert.ok(page.startsWith('Run '), page)
  assert.ok(page.includes("Fits the model on the sources' sample, without the nodes after it"))
  assert.ok(!page.includes('Last run'))
})

test('a flow with changes is saved first; a trial in progress counts its seconds and cannot start twice', async () => {
  assert.ok(text(await render(Run, { flowDirty: true })).startsWith('Save and run'))
  const html = await render(Run, { trial: { runId: 9, status: 'STARTED', startedAt: Date.now() - 4000, error: '' }, outputs: { last: last(), output: null } })
  assert.match(text(html), /^Running… [45] s/)
  assert.ok(button(html).includes('disabled'))
  assert.ok(!text(html).includes('Last run'), 'the latest outcome waits for the trial')
})

test('the latest trial: its outcome, its rows, its log; «not up to date» when the node or its chain changed', async () => {
  const output = { run_id: 7, bucket: 'data-prep', key: 'out/r.parquet', rows: 1234, trial: true, finished_at: hourAgo, stale: false }
  let page = text(await render(Run, { outputs: { last: last({ log: 'fitted' }), output } }))
  assert.ok(/Last run from here .+: SUCCESS · 1,234 rows/.test(page), page)
  assert.ok(page.includes('Log fitted'))
  assert.ok(!page.includes('changed since'))
  page = text(await render(Run, { outputs: { last: last({ trial: false, stale: true }), output: { ...output, stale: true } } }))
  assert.ok(page.includes('Last run of the flow'))
  assert.ok(page.includes('The model or a step before it changed since: run it again'))
})

test('a refusal says the runner\'s sentence; a trial refused before it ran says the gateway\'s', async () => {
  const refused = last({ status: 'FAILURE', error: 'model «Elasticity»: the outcome is separated by price: use Firth', log: 'TABULARIA_ERROR {…}' })
  let html = await render(Run, { outputs: { last: refused, output: null }, trial: { runId: 7, status: 'FAILURE', startedAt: Date.now(), error: refused.error } })
  let page = text(html)
  assert.ok(page.includes(': FAILURE'))
  assert.equal(page.split('the outcome is separated by price').length - 1, 1, 'said once, by the run')
  html = await render(Run, { trial: { runId: null, status: 'FAILURE', startedAt: Date.now(), error: 'You may not use the model group «regression»' } })
  page = text(html)
  assert.ok(html.includes('role="alert"') && page.includes('You may not use the model group'))
})

test('a node that cannot run: the button is off, and says why when the form does not', async () => {
  const html = await render(Run, { disabled: true, why: 'Set its columns in the panel.' })
  assert.ok(button(html).includes('disabled'))
  assert.ok(text(html).includes('Set its columns in the panel.'))
  assert.ok(button(await render(Run, { readOnly: true })).includes('disabled'), 'an observer does not run')
})

test('the words of Run in the five languages', async () => {
  for (const lang of LANGS) {
    const page = text(await render(Run, { outputs: { last: last({ stale: true }), output: null } }, lang))
    assert.ok(!/models\.node\./.test(page), `${lang}: ${page}`)
  }
})

test('the panels hand the button what the editor knows, and the editor runs it as a Script node\'s trial', () => {
  const node = read('components/ui/NodePanel.vue')
  const block = node.slice(node.indexOf('<ModelNodePanel'), node.indexOf('/>', node.indexOf('<ModelNodePanel')))
  for (const piece of [':outputs="scriptOutputs?.[node.id] ?? null"', ':trial="scriptTrials?.[node.id] ?? null"', ':flow-dirty="!!flowDirty"',
    '@run="emit(\'script-run\', node.id)"']) assert.ok(block.includes(piece), piece)
  const panel = read('components/models/ModelNodePanel.vue')
  assert.ok(panel.includes('<template #run>') && panel.includes('<ModelRun') && panel.includes("@run=\"emit('run')\""))
  const editor = read('components/FlowEditor.vue')
  assert.ok(editor.includes('@script-run="runScriptNode"'))
  assert.match(editor, /STEP_TYPES = new Set\(\[[^\]]*'model'/)
})

test('a Predict node has Run too, once it names its connection and its version; its runs show on the run page', () => {
  const panel = read('components/models/ModelNodePanel.vue')
  const predict = panel.slice(panel.indexOf('<PredictForm'), panel.indexOf('</PredictForm>'))
  assert.ok(predict.includes('<template #run>') && predict.includes('<ModelRun') && predict.includes(':disabled="!predictReady"'), predict)
  assert.match(panel, /predictReady = computed\(\(\) => typeof props\.node\.data\?\.connectionId === 'number' && !!props\.node\.data\?\.registeredModel && !!props\.node\.data\?\.version\)/)
  assert.ok(read('components/ui/PredictForm.vue').includes('<slot name="run" />'))
  assert.match(read('components/FlowEditor.vue'), /STEP_TYPES = new Set\(\[[^\]]*'model_predict'/)
  const runs = read('pages/runs.vue')
  assert.ok(runs.includes(`(r.kind === 'model' || r.kind === 'model_predict') && r.script`))
  assert.ok(runs.includes(`r.kind === 'model_predict' ? t('models.predict.title')`))
  for (const lang of LANGS) assert.ok(MESSAGES[lang].models.predict.chooseModel && MESSAGES[lang].models.predict.title, lang)
})
