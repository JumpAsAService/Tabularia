// The MLflow section of the Bayesian model node's panel (docs/specs/bayes-mlflow.md, lot M3), without a browser: the block it
// writes (the Model node's object, a registered name that goes with the connection), the section's states drawn on the server,
// the places it is wired into and the one new sentence in the five languages.
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
const OUT = join(FRONTEND, 'tests', `.rendered-bayes-mlflow-${process.pid}`)
mkdirSync(OUT, { recursive: true })
after(() => rmSync(OUT, { recursive: true, force: true }))

Reflect.set(globalThis, 'useApiClient', () => ({ apiFetch: async () => { throw new Error('no gateway here') } }))
const B = await import('../composables/bayesMlflow.ts')
const read = (p) => readFileSync(join(FRONTEND, p), 'utf8')
const LANGS = ['en', 'it', 'de', 'es', 'fr']
const MESSAGES = {}
const WORDS = {}
const flat = (tree, prefix = '', out = {}) => {
  for (const [k, v] of Object.entries(tree)) typeof v === 'string' ? (out[prefix + k] = v) : flat(v, `${prefix}${k}.`, out)
  return out
}
for (const lang of LANGS) {
  const gen = (await import(`../i18n/locales/${lang}.gen.ts`)).default
  MESSAGES[lang] = { ...(await import(`../i18n/locales/${lang}.ts`)).default, ...gen }
  WORDS[lang] = flat(gen)
}

// ── what the panel writes ────────────────────────────────────────────────────

test('a connection chosen: an experiment still to name; none: the whole block goes, the registered name with it', () => {
  assert.deepEqual(B.bayesMlflowConnection(null, 7), { connectionId: 7, experiment: '' })
  assert.equal(B.bayesMlflowProblem({ connectionId: 7, experiment: '' }), 'experiment')
  assert.equal(B.bayesMlflowConnection({ connectionId: 7, experiment: 'risk', registerAs: 'm' }, null), undefined)
  assert.equal(B.bayesMlflowConnection({ connectionId: 7, experiment: 'risk', registerAs: 'm' }, undefined), undefined)
  // another server keeps what was typed
  assert.deepEqual(B.bayesMlflowConnection({ connectionId: 7, experiment: 'risk', registerAs: 'm', continueWithout: true }, 9),
    { connectionId: 9, experiment: 'risk', registerAs: 'm', continueWithout: true })
})

test('the fields: names trimmed, an empty registered name gone, «Continue without MLflow» kept only when on', () => {
  const base = { connectionId: 3, experiment: 'risk' }
  assert.deepEqual(B.bayesMlflowSet(base, 'experiment', '  Pasta/price  '), { connectionId: 3, experiment: 'Pasta/price' })
  assert.deepEqual(B.bayesMlflowSet(base, 'registerAs', ' hours-model '), { connectionId: 3, experiment: 'risk', registerAs: 'hours-model' })
  assert.deepEqual(B.bayesMlflowSet({ ...base, registerAs: 'x' }, 'registerAs', '   '), base)
  assert.deepEqual(B.bayesMlflowSet(base, 'continueWithout', true), { ...base, continueWithout: true })
  assert.deepEqual(B.bayesMlflowSet({ ...base, continueWithout: true }, 'continueWithout', false), base)
  assert.equal(B.bayesMlflowProblem(B.bayesMlflowSet(base, 'experiment', '   ')), 'experiment')
  // the gateway reads exactly these four keys
  assert.deepEqual(Object.keys(B.bayesMlflowSet({ ...base, registerAs: 'a', continueWithout: true }, 'experiment', 'e')).sort(),
    ['connectionId', 'continueWithout', 'experiment', 'registerAs'])
})

// ── what is drawn ────────────────────────────────────────────────────────────

async function component (path) {
  const { descriptor, errors } = parse(read(path), { filename: path })
  assert.deepEqual(errors, [])
  const content = compileScript(descriptor, { id: path, inlineTemplate: true }).content
    .replace("import Select from '~/components/ui/Select.vue'", 'const Select = globalThis.__SelectStub')
  const file = join(OUT, `${path.replace(/[^\w]/g, '_')}.ts`)
  writeFileSync(file, content)
  return (await import(pathToFileURL(file).href)).default
}
const Select = defineComponent({
  name: 'Select',
  inheritAttrs: false,
  props: ['modelValue', 'options', 'disabled'],
  setup: (props, { attrs }) => () => h('div', { class: 'select', 'data-value': String(props.modelValue ?? ''), ...attrs },
    (props.options ?? []).map((o) => h('span', { class: 'option' }, o.label))),
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
const text = (html) => html.replace(/<!--.*?-->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').replace(/&#39;/g, "'").trim()
const CONNECTIONS = [{ id: 3, name: 'Tracking', db_type: 'mlflow' }, { id: 4, name: 'Warehouse', db_type: 'postgres' }]
Reflect.set(globalThis, '__SelectStub', Select)
const props = (more = {}) => ({ nodeId: 'bayes-1', connections: CONNECTIONS, ...more })

test('the section without a connection: the choice and its hint, nothing else', async () => {
  const Mlflow = await component('components/bayes/BayesMlflow.vue')
  const html = await render(Mlflow, props())
  const page = text(html)
  assert.ok(page.includes('MLflow connection') && page.includes('No MLflow: the results stay with the run') && page.includes('Tracking'), page)
  assert.ok(!page.includes('Warehouse'), 'only the MLflow connections are offered')
  assert.ok(page.includes('Log each run of the flow to an MLflow server'), page)
  assert.ok(!page.includes('Experiment') && !page.includes('Continue without MLflow') && !page.includes('go to MLflow'), page)
  // a person with no connection of the kind is told so
  assert.ok(text(await render(Mlflow, props({ connections: [CONNECTIONS[1]] }))).includes('No MLflow connection is available to you'))
})

test('a connection and no experiment: the problem sentence, the fields, the note on what goes to MLflow', async () => {
  const Mlflow = await component('components/bayes/BayesMlflow.vue')
  const html = await render(Mlflow, props({ mlflow: { connectionId: 3, experiment: '' } }))
  const page = text(html)
  for (const words of ['Experiment', 'Give the experiment a name.', 'Register the model as', 'Continue without MLflow',
    "The parameters, the summary of the quantities, the sampler's facts, the report and the model's files go to MLflow; the draws never do.",
    'A Predict step does not apply it.']) assert.ok(page.includes(words), `«${words}» is not on the page: ${page}`)
  assert.match(html, /aria-invalid="true"/)
  assert.match(html, /role="alert"/)
  assert.ok(!page.includes('Each run adds a version of the model under this name, which a Predict step can apply'), 'the Model node\'s hint would promise a Predict')
  assert.ok(!page.includes('Log each run of the flow'), page)
  // with an experiment: no problem, the switch is on when the block says so
  const ok = await render(Mlflow, props({ mlflow: { connectionId: 3, experiment: 'risk', registerAs: 'hours', continueWithout: true } }))
  assert.ok(!text(ok).includes('Give the experiment a name.') && !/aria-invalid/.test(ok))
  assert.match(ok, /role="switch"[^>]*checked|checked[^>]*role="switch"/)
  assert.match(ok, /value="hours"/)
})

test('a connection the person cannot use any more stays listed, and a read-only node disables the section', async () => {
  const Mlflow = await component('components/bayes/BayesMlflow.vue')
  const gone = text(await render(Mlflow, props({ mlflow: { connectionId: 99, experiment: 'x' } })))
  assert.ok(gone.includes('Connection #99 (not available to you)'), gone)
  assert.match(await render(Mlflow, props({ readOnly: true })), /<fieldset[^>]*disabled/)
})

test('the five languages draw the section with their own words', async () => {
  const Mlflow = await component('components/bayes/BayesMlflow.vue')
  const seen = new Set()
  for (const lang of LANGS) {
    const page = text(await render(Mlflow, props({ mlflow: { connectionId: 3, experiment: '' } }), lang))
    assert.ok(page.includes(WORDS[lang]['bayesNode.mlflowNote']), lang)
    assert.ok(page.includes(MESSAGES[lang].models.mlflow.experimentNeeded), lang)
    seen.add(WORDS[lang]['bayesNode.mlflowNote'])
  }
  assert.equal(seen.size, 5, 'each language has its own sentence')
})

// ── the places it is wired into ──────────────────────────────────────────────

test('the panel mounts the section between the output and the limits, with the person\'s connections; ModelForm is not involved', () => {
  const panel = read('components/bayes/BayesNodePanel.vue')
  const section = panel.indexOf('<BayesMlflow')
  assert.ok(panel.indexOf('<!-- 5. the output -->') < section && section < panel.indexOf('<!-- 6. the limits -->'))
  assert.match(panel, /<BayesMlflow[^>]*:mlflow="data\.mlflow"[^>]*:connections="connections"/)
  assert.match(read('components/ui/NodePanel.vue'), /<BayesNodePanel[^>]*:connections="connections"/s)
  assert.ok(!/ModelForm/.test(read('components/bayes/BayesMlflow.vue').replace(/^\s*\/\/.*$/gm, '')), 'a shared form would change every other group\'s')
})

test('the one sentence and its companion: in the five languages, same placeholders, nothing of vue-i18n\'s syntax, no verdict', () => {
  for (const lang of LANGS) {
    for (const key of ['bayesNode.mlflowNote', 'bayesNode.mlflowRegisterHint']) {
      const v = WORDS[lang][key]
      assert.ok(typeof v === 'string' && v.length > 30, `${lang} ${key}`)
      assert.ok(!/[{}|@]/.test(v), `${lang} ${key}: ${v}`)
    }
  }
})
