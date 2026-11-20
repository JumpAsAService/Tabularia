// The statistical models in the editor without a browser (composables/modelCatalog.ts and the
// components drawn from it): the catalogue's copy is the engine's snapshot, every model has a
// form the editor can draw and its words in the five languages, the palette shows only the groups
// the administrator turned on (and says «needs Kubernetes» on Compose), the form shows only the keys
// that apply to the variant, what is saved holds only those keys (the gateway refuses the others),
// what a variant forbids is known to the form, the six ARIMA presets are the same node. A second
// model is tried on a catalogue made here: nothing in the editor names a model.
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
const OUT = join(FRONTEND, 'tests', `.rendered-models-${process.pid}`)
mkdirSync(OUT, { recursive: true })
after(() => rmSync(OUT, { recursive: true, force: true }))

// what Nuxt auto-imports: the API client, asked by the composable that lists the groups
const asked = []
// (Reflect.set, not `globalThis.useApiClient = …`: the type check reads this file too, and an assignment to
// globalThis there declares a global that takes the place of the real useApiClient in every composable)
Reflect.set(globalThis, 'useApiClient', () => ({ apiFetch: async (path) => { asked.push(path); throw new Error('no gateway here') } }))

const m = await import('../composables/modelCatalog.ts')
const { opMeta } = await import('../composables/useOpIcons.ts')
void opMeta

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const LANGS = ['en', 'it', 'de', 'es', 'fr']
const flatten = (tree, prefix = '', out = {}) => {
  for (const [k, v] of Object.entries(tree)) {
    if (typeof v === 'string') out[prefix + k] = v
    else flatten(v, `${prefix}${k}.`, out)
  }
  return out
}
const MESSAGES = {}
const WORDS = {}
for (const lang of LANGS) {
  MESSAGES[lang] = { ...(await import(`../i18n/locales/${lang}.ts`)).default, ...(await import(`../i18n/locales/${lang}.gen.ts`)).default }
  WORDS[lang] = flatten((await import(`../i18n/locales/${lang}.gen.ts`)).default)
}
const en = (key) => WORDS.en[key] ?? key
const has = (key) => key in WORDS.en

const LINEAR = m.modelOf('linear')
const LOGISTIC = m.modelOf('logistic')
const KM = m.modelOf('kaplan_meier')
const COX = m.modelOf('cox')
const ARIMA = m.modelOf('arima')

// ── the catalogue ────────────────────────────────────────────────────────────

test('the catalogue the editor reads is the engine\'s snapshot, byte for byte', () => {
  assert.equal(read('../assets/model-catalog.json'), read('../../backend/app/engine/models/catalog.json'),
    'run python3 .github/scripts/model_catalog.py')
  assert.deepEqual(m.MODEL_CATALOG.models.map((x) => x.id), ['linear', 'logistic', 'kaplan_meier', 'cox', 'arima', 'xgboost', 'catboost'])
  assert.deepEqual(m.MODEL_CATALOG.groups.map((g) => g.id), ['regression', 'survival', 'timeseries', 'boosting'])
})

test('every key of every model has a control the form draws', () => {
  assert.deepEqual([...m.MODEL_CONTROLS].sort(), [...m.MODEL_CATALOG.controls].sort())
  const form = read('../components/ui/ModelForm.vue')
  const predict = read('../components/ui/PredictForm.vue')
  for (const control of m.MODEL_CATALOG.controls) {
    assert.ok(form.includes(`f.control === '${control}'`), `ModelForm.vue does not draw «${control}»`)
  }
  for (const control of ['boolean', 'numbers', 'number', 'integer']) assert.ok(predict.includes(control) || control === 'integer', `PredictForm.vue: ${control}`)
  for (const model of m.MODEL_CATALOG.models) {
    for (const f of m.modelFields(model)) {
      assert.ok(m.MODEL_CONTROLS.includes(f.control), `${model.id}.${f.key}`)
      if (f.control === 'level' || f.control === 'reference' || f.control === 'pairs') {
        assert.ok(f.of in { ...model.needs, ...model.takes }, `${model.id}.${f.key} is of «${f.of}»`)
      }
    }
  }
  for (const key of Object.keys(m.MODEL_CATALOG.predict.takes)) {
    const kind = m.MODEL_CATALOG.kinds[m.MODEL_CATALOG.predict.takes[key]]
    assert.ok(['boolean', 'number', 'integer', 'numbers'].includes(kind.control), `predict.${key}`)
  }
})

test('the fields of a model in the catalogue\'s order, what it needs first', () => {
  assert.deepEqual(m.modelFields(KM).map((f) => [f.key, f.control, f.required]).slice(0, 5), [
    ['time', 'column', true], ['event', 'column', true], ['event_value', 'level', true], ['strata', 'column', false], ['conf_type', 'choice', false],
  ])
  const by = Object.fromEntries(m.modelFields(LINEAR).map((f) => [f.key, f]))
  assert.equal(by.outcome.numeric, true)
  assert.equal(by.predictors.predictor, true)
  assert.equal(by.lambda.control, 'number_or_choice')
  assert.deepEqual(by.penalty.choices, ['none', 'ridge', 'lasso', 'elastic_net'])
  assert.equal(m.modelFields(ARIMA).find((f) => f.key === 'time').types.length, 3)
})

test('every model, group, preset, field, choice and reason has its words in the five languages', () => {
  const wanted = m.modelWordKeys(m.MODEL_CATALOG)
  for (const key of ['models.groups.regression', 'models.linear.name', 'models.arima.hint', 'models.presets.sarimax.name',
    'models.params.outcome', 'models.choice.firth', 'models.reasons.firth_needs_logit', 'predict.params.at_times']) assert.ok(wanted.includes(key), key)
  const missing = []
  for (const lang of LANGS) for (const key of wanted) if (!WORDS[lang][key]?.trim()) missing.push(`${lang} ${key}`)
  assert.deepEqual(missing, [])
  // the same words in every language: a placeholder one has, all have; no key one lacks
  const keys = Object.keys(WORDS.en).filter((k) => k.startsWith('models.') || k.startsWith('predict.'))
  for (const lang of LANGS) {
    const own = Object.keys(WORDS[lang]).filter((k) => k.startsWith('models.') || k.startsWith('predict.'))
    assert.deepEqual(own.sort(), [...keys].sort(), lang)
    for (const key of keys) {
      const holes = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((x) => x[1]).sort().join()
      assert.equal(holes(WORDS[lang][key]), holes(WORDS.en[key]), `${lang} ${key}`)
    }
  }
  // the words the form and the nodes ask for by name
  const used = new Set()
  for (const file of ['../components/ui/ModelForm.vue', '../components/ui/PredictForm.vue', '../components/nodes/ModelNode.vue',
    '../components/nodes/PredictNode.vue', '../components/models/ModelNodePanel.vue', '../components/ui/OpSidebar.vue']) {
    for (const x of read(file).matchAll(/\bt\('((?:models|predict)\.[\w.]+)'|\$t\('((?:models|predict)\.[\w.]+)'/g)) used.add(x[1] ?? x[2])
  }
  assert.ok(used.size > 40)
  assert.deepEqual([...used].filter((k) => !has(k)), [])
})

test('a model added to the catalogue is asked for its words by name', () => {
  const TWO = {
    ...m.MODEL_CATALOG,
    groups: [...m.MODEL_CATALOG.groups, { id: 'zz_group', language: 'r', label: 'models.groups.zz_group', models: ['zz_poisson'] }],
    kinds: { ...m.MODEL_CATALOG.kinds, zz_offset: { control: 'choice', choices: ['none', 'exposure'] } },
    models: [...m.MODEL_CATALOG.models, {
      id: 'zz_poisson', group: 'zz_group', family: 'regression', label: 'models.zz_poisson.name',
      needs: { outcome: 'number_column' }, takes: { zz_offset: 'zz_offset' }, defaults: { zz_offset: 'none' },
      applies: {}, requires: [], forbids: [{ key: 'zz_offset', value: 'exposure', when: { zz_offset: 'none' }, reason: 'zz_nope', message: 'no' }],
      disjoint: [], presets: {}, variants: [{ id: 'default', when: {} }],
    }],
  }
  const lacking = m.modelWordKeys(TWO).filter((k) => !(k in WORDS.en))
  assert.deepEqual(lacking, [
    'models.groups.zz_group', 'models.zz_poisson.name', 'models.zz_poisson.hint', 'models.params.zz_offset', 'models.choice.exposure', 'models.reasons.zz_nope',
  ])
  // and the editor draws it: its palette entry, its fields, its defaults
  const shown = TWO.groups.filter((g) => g.id === 'zz_group')
  assert.deepEqual(m.modelPalette(shown, '', en, TWO).map((g) => g.items.map((i) => i.kind)), [['model:zz_poisson']])
  assert.deepEqual(m.modelFields(TWO.models.at(-1), TWO).map((f) => f.key), ['outcome', 'zz_offset'])
  assert.deepEqual(m.modelDefaults('zz_poisson', undefined, TWO), { zz_offset: 'none' })
})

// ── the palette ──────────────────────────────────────────────────────────────

const group = (id, enabled = true, usable = enabled) => ({ id, enabled, usable, ready: enabled })
const STATE = { disabled_reason: null, groups: [group('regression'), group('survival', false), group('timeseries')] }

test('the palette shows the groups the person may use: not a disabled one, not one they may not use', () => {
  assert.deepEqual(m.modelShownGroups(STATE).map((g) => g.id), ['regression', 'timeseries'])
  assert.deepEqual(m.modelShownGroups({ disabled_reason: null, groups: [group('regression', true, false), group('survival')] }).map((g) => g.id), ['survival'])
  // none enabled, or the route has not answered, or the machinery is missing: nothing
  assert.deepEqual(m.modelShownGroups({ disabled_reason: null, groups: [group('regression', false)] }), [])
  assert.deepEqual(m.modelShownGroups(null), [])
  assert.deepEqual(m.modelShownGroups({ disabled_reason: 'not_configured', groups: [] }), [])
  // Compose: all of them, to be shown greyed out
  assert.equal(m.modelShownGroups({ disabled_reason: 'kubernetes', groups: [] }).length, 4)
  assert.equal(m.modelOff('linear' && 'regression', { disabled_reason: 'kubernetes', groups: [] }), 'kubernetes')
  assert.equal(m.modelOff('survival', STATE), 'not_enabled')
  assert.equal(m.modelOff('regression', STATE), '')
  assert.equal(m.modelOff('regression', { disabled_reason: null, groups: [group('regression', true, false)] }), 'not_allowed')
  assert.equal(m.modelOff('regression', null), 'unknown')
})

test('the palette\'s entries: the models by group, the six presets after ARIMA, a search, the drag\'s kinds', () => {
  const all = m.modelPalette(m.MODEL_CATALOG.groups, '', en)
  assert.deepEqual(all.map((g) => g.group), ['regression', 'survival', 'timeseries', 'boosting'])
  assert.deepEqual(all[0].items.map((i) => i.kind), ['model:linear', 'model:logistic'])
  assert.deepEqual(all[1].items.map((i) => i.kind), ['model:kaplan_meier', 'model:cox'])
  assert.deepEqual(all[2].items.map((i) => i.kind), ['model:arima', 'model:arima:ar', 'model:arima:ma', 'model:arima:arma', 'model:arima:arima', 'model:arima:sarima', 'model:arima:sarimax'])
  assert.deepEqual(m.modelPalette(m.modelShownGroups(STATE), '', en).map((g) => g.group), ['regression', 'timeseries'])
  // a search by name, by hint, by group
  assert.deepEqual(m.modelPalette(m.MODEL_CATALOG.groups, 'cox', en).flatMap((g) => g.items.map((i) => i.model)), ['cox'])
  // SARIMAX is a preset of the ARIMA node, and ARIMA's hint says it
  assert.deepEqual(m.modelPalette(m.MODEL_CATALOG.groups, 'sarimax', en).flatMap((g) => g.items.map((i) => i.kind)), ['model:arima', 'model:arima:sarimax'])
  assert.ok(m.modelPalette(m.MODEL_CATALOG.groups, 'survival', en).some((g) => g.group === 'survival' && g.items.length === 2))
  assert.deepEqual(m.modelPalette(m.MODEL_CATALOG.groups, 'zzzz', en), [])
  // what a drag carries is read back, and nothing else is a model
  assert.deepEqual(m.modelFromKind('model:arima:sarima'), { model: 'arima', preset: 'sarima' })
  assert.deepEqual(m.modelFromKind('model:linear'), { model: 'linear' })
  for (const bad of ['op:filter', 'model:', 'model:nope', 'model:linear:ar', 'script', 'model_predict', 'stat_t_two_sample']) assert.equal(m.modelFromKind(bad), null, bad)
  // Predict: for whoever may use a group and has an MLflow connection
  assert.equal(m.predictAvailable(STATE, 1), true)
  assert.equal(m.predictAvailable(STATE, 0), false)
  assert.equal(m.predictAvailable({ disabled_reason: null, groups: [group('regression', false)] }, 2), false)
})

// ── the parameters ───────────────────────────────────────────────────────────

test('a new node starts with the defaults of the keys that apply, and says nothing it has no value for', () => {
  assert.deepEqual(m.modelDefaults('linear'), { estimation: 'ols', penalty: 'none', intercept: true, se: 'classical', alpha: 0.05, seed: 1, output: 'results' })
  assert.deepEqual(m.modelDefaults('kaplan_meier'), { conf_type: 'log', alpha: 0.05, seed: 1, output: 'results' })
  const data = m.modelNodeData('linear')
  assert.deepEqual(Object.keys(data).sort(), ['model', 'params', 'retries', 'timeoutSeconds'])
  assert.deepEqual(Object.keys(data).filter((k) => !m.MODEL_CATALOG.nodes.model.data.includes(k)), [])
  assert.deepEqual(Object.keys(m.predictNodeData()).filter((k) => !m.MODEL_CATALOG.nodes.model_predict.data.includes(k)), [])
})

test('turning a penalty on takes away what the unpenalized variant uses, and the other way round', () => {
  let p = m.modelDefaults('linear')
  p = m.modelSet(LINEAR, p, 'se', 'cluster')
  p = m.modelSet(LINEAR, p, 'cluster', 'store')
  p = m.modelSet(LINEAR, p, 'order_by', 'day')
  assert.deepEqual([p.se, p.cluster, p.order_by, p.estimation], ['cluster', 'store', 'day', 'ols'])
  assert.equal(m.modelVariant(LINEAR, p), 'unpenalized')
  // a penalty: se, its cluster column, the order, the estimation go; λ, folds and the rest are the form's now
  p = m.modelSet(LINEAR, p, 'penalty', 'ridge')
  assert.deepEqual(Object.keys(p).sort(), ['alpha', 'intercept', 'output', 'penalty', 'seed'])
  assert.equal(m.modelVariant(LINEAR, p), 'penalized')
  for (const key of ['se', 'cluster', 'order_by', 'estimation']) assert.equal(m.modelApplies(LINEAR, key, p), false, key)
  for (const key of ['lambda', 'folds', 'fold_by', 'measure', 'standardize']) assert.equal(m.modelApplies(LINEAR, key, p), true, key)
  assert.equal(m.modelApplies(LINEAR, 'mixing', p), false)
  assert.equal(m.modelApplies(LINEAR, 'mixing', m.modelSet(LINEAR, p, 'penalty', 'elastic_net')), true)
  p = m.modelSet(LINEAR, p, 'lambda', 'cv_min')
  p = m.modelSet(LINEAR, p, 'folds', '5')
  assert.deepEqual([p.lambda, p.folds], ['cv_min', 5])
  // back: the penalty's keys go
  p = m.modelSet(LINEAR, p, 'penalty', 'none')
  assert.ok(!('lambda' in p) && !('folds' in p) && p.penalty === 'none')
})

test('whatever is saved is a set of keys that apply: the gateway refuses the others', () => {
  // every combination of the keys that rule another, with a value in every key of the model
  for (const model of m.MODEL_CATALOG.models) {
    const rulers = new Set(Object.values(model.applies).flatMap((r) => Object.keys(r)))
    const choices = [...rulers].map((k) => {
      const f = m.modelFields(model).find((x) => x.key === k)
      if (f.control === 'choice') return f.choices.map((c) => [k, c])
      if (f.control === 'boolean') return [[k, true], [k, false]]
      if (f.control === 'column') return [[k, 'a'], [k, undefined]]
      if (f.control === 'columns') return [[k, ['a']], [k, []]]
      return [[k, undefined]]
    })
    let combos = [{}]
    for (const options of choices) combos = combos.flatMap((c) => options.map(([k, v]) => ({ ...c, [k]: v })))
    for (const combo of combos) {
      // everything the model takes, at its default or a plausible value, so that nothing is absent by luck
      const all = { ...Object.fromEntries(Object.entries(model.defaults).map(([k, v]) => [k, v])), ...combo }
      for (const f of m.modelFields(model)) {
        if (all[f.key] === undefined || all[f.key] === null || (Array.isArray(all[f.key]) && !all[f.key].length)) {
          if (f.control === 'column') all[f.key] = 'a'
          else if (f.control === 'columns') all[f.key] = ['a']
          else if (f.control === 'integer') all[f.key] = f.min ?? 1
          else if (f.control === 'number') all[f.key] = 0.5
          else if (f.control === 'choice') all[f.key] = f.choices[0]
          else if (f.control === 'boolean') all[f.key] = true
          else if (f.control === 'numbers') all[f.key] = [1]
          else if (f.control === 'number_or_choice') all[f.key] = f.choices[0]
        }
        if (combo[f.key] === undefined && f.key in combo) delete all[f.key]
      }
      const saved = m.modelParams(model, all)
      for (const key of Object.keys(saved)) assert.ok(m.modelApplies(model, key, saved), `${model.id} ${JSON.stringify(combo)}: «${key}» does not apply`)
      // saving what is saved changes nothing
      assert.deepEqual(m.modelParams(model, saved), saved)
    }
  }
})

test('Kaplan–Meier: the test and the pairs need strata; Cox: the cluster needs robust errors', () => {
  const base = { time: 't', event: 'e', event_value: '1', test: 'peto', pairwise: true }
  assert.deepEqual(m.modelParams(KM, base), { time: 't', event: 'e', event_value: '1' })
  assert.deepEqual(m.modelParams(KM, { ...base, strata: 'arm' }), { time: 't', event: 'e', event_value: '1', strata: 'arm', test: 'peto', pairwise: true })
  assert.equal(m.modelApplies(COX, 'cluster', { se: 'classical' }), false)
  assert.equal(m.modelApplies(COX, 'cluster', { se: 'robust' }), true)
  assert.ok(!('cluster' in m.modelParams(COX, { time: 't', event: 'e', event_value: 1, predictors: ['x'], cluster: 'id' })))
})

test('ARIMA: orders = auto hides the orders and shows their maxima; an exogenous column takes the horizon away', () => {
  const manual = { time: 'day', value: 'qty', frequency: 'month', p: 2, d: 1, q: 1, max_p: 3, criterion: 'bic' }
  let p = m.modelParams(ARIMA, manual)
  assert.deepEqual(p, { time: 'day', value: 'qty', frequency: 'month', p: 2, d: 1, q: 1 })
  p = m.modelSet(ARIMA, { ...manual, p: 2 }, 'orders', 'auto')
  assert.ok(!('p' in p) && !('d' in p) && !('q' in p), JSON.stringify(p))
  assert.deepEqual([p.max_p, p.criterion, p.orders], [3, 'bic', 'auto'])
  for (const key of ['stepwise', 'max_P', 'unit_root_test', 'seasonal_test']) assert.equal(m.modelApplies(ARIMA, key, p), true, key)
  assert.equal(m.modelApplies(ARIMA, 'p', p), false)
  // the horizon is the rows after the last one when there are exogenous columns
  assert.equal(m.modelApplies(ARIMA, 'horizon', { orders: 'manual' }), true)
  assert.equal(m.modelApplies(ARIMA, 'horizon', { exog: ['price'] }), false)
  assert.ok(!('horizon' in m.modelSet(ARIMA, { ...manual, horizon: 24 }, 'exog', ['price'])))
  // Box–Cox's λ
  assert.equal(m.modelApplies(ARIMA, 'box_cox_lambda', { transform: 'log' }), false)
  assert.equal(m.modelApplies(ARIMA, 'box_cox_lambda', { transform: 'box_cox' }), true)
})

test('the six ARIMA presets are the one node with the orders they leave free, and its title says which model it is', () => {
  assert.deepEqual(Object.keys(ARIMA.presets), ['ar', 'ma', 'arma', 'arima', 'sarima', 'sarimax'])
  for (const preset of Object.keys(ARIMA.presets)) {
    const data = m.modelNodeData('arima', preset)
    assert.equal(data.model, 'arima')
    assert.deepEqual(Object.keys(data).filter((k) => !m.MODEL_CATALOG.nodes.model.data.includes(k)), [], preset)
    assert.equal(data.params.orders, 'manual')
    for (const [k, v] of Object.entries(ARIMA.presets[preset])) assert.equal(data.params[k], v, `${preset}.${k}`)
  }
  const title = (preset, more = {}) => m.modelArimaTitle({ ...m.modelNodeData('arima', preset).params, ...more })
  assert.equal(title('ar'), 'AR(1)')
  assert.equal(title('ma'), 'MA(1)')
  assert.equal(title('arma'), 'ARMA(1,1)')
  assert.equal(title('arima'), 'ARIMA(1,1,1)')
  assert.equal(title('sarima', { frequency: 'month' }), 'SARIMA(1,1,1)(0,1,1)[12]')
  assert.equal(title('sarima', { frequency: 'quarter' }), 'SARIMA(1,1,1)(0,1,1)[4]')
  assert.equal(title('sarima', { s: 7 }), 'SARIMA(1,1,1)(0,1,1)[7]')
  assert.equal(title('sarimax', { frequency: 'month', exog: ['price'] }), 'SARIMAX(1,1,1)(0,1,1)[12]')
  assert.equal(title('ar', { p: 3, exog: ['price'] }), 'ARX(3)')
  assert.equal(title('arima', { orders: 'auto' }), 'ARIMA (auto)')
  // changing an order changes the model: no selector
  assert.equal(title('arima', { d: 0 }), 'ARMA(1,1)')
  assert.equal(title('arima', { d: 0, q: 0 }), 'AR(1)')
  assert.equal(m.modelSeason({ frequency: 'week' }), 52)
  assert.equal(m.modelSeason({}), null)
})

test('the weights, the cluster and the reference: what a variant needs, forbids and refers to', () => {
  // WLS needs the weights, clustered errors the column
  let need = m.modelUnset(LINEAR, { outcome: 'y', predictors: ['x'], estimation: 'wls' })
  assert.deepEqual(need, ['weights'])
  assert.deepEqual(m.modelUnset(LINEAR, { outcome: 'y', predictors: ['x'], estimation: 'wls', weights: 'w' }), [])
  assert.deepEqual(m.modelUnset(LINEAR, { outcome: 'y', predictors: ['x'], se: 'cluster' }), ['cluster'])
  assert.deepEqual(m.modelUnset(LINEAR, {}), ['outcome', 'predictors'])
  // a penalty makes WLS no variant at all: nothing is needed, the weights are case weights
  assert.deepEqual(m.modelUnset(LINEAR, { outcome: 'y', predictors: ['x'], estimation: 'wls', penalty: 'lasso' }), [])
  assert.equal(m.modelRequiredReason(LINEAR, 'weights', { estimation: 'wls' }).reason, 'wls_needs_weights')
  assert.deepEqual(m.modelUnset(KM, { time: 't', event: 'e' }), ['event_value'])
  assert.deepEqual(m.modelUnset(ARIMA, { time: 'day', value: 'qty' }), ['frequency'])
  // Firth needs the logit link; the profile intervals the classical errors (the lists of `when`)
  assert.equal(m.modelForbidden(LOGISTIC, 'estimation', 'firth', { link: 'probit' })?.reason, 'firth_needs_logit')
  assert.equal(m.modelForbidden(LOGISTIC, 'estimation', 'firth', {}), null)
  assert.equal(m.modelForbidden(LOGISTIC, 'estimation', 'ml', { link: 'probit' }), null)
  for (const se of ['hc0', 'hc1', 'hc2', 'hc3', 'cluster']) assert.equal(m.modelForbidden(LOGISTIC, 'ci', 'profile', { se })?.reason, 'profile_needs_classical_se', se)
  assert.equal(m.modelForbidden(LOGISTIC, 'ci', 'profile', { se: 'classical' }), null)
  assert.equal(m.modelForbidden(LOGISTIC, 'ci', 'wald', { se: 'hc3' }), null)
})

test('a column is one thing only; a level, a reference and an interaction follow the columns they are of', () => {
  assert.deepEqual(m.modelOverlaps(LINEAR, { outcome: 'y', predictors: ['x', 'y'] }), [{ column: 'y', keys: ['outcome', 'predictors'] }])
  assert.deepEqual(m.modelOverlaps(LINEAR, { outcome: 'y', predictors: ['x'], weights: 'x' }), [{ column: 'x', keys: ['predictors', 'weights'] }])
  assert.deepEqual([...m.modelTaken(LINEAR, { outcome: 'y', predictors: ['x'] }, 'weights')].sort(), ['x', 'y'])
  assert.deepEqual([...m.modelTaken(LINEAR, { outcome: 'y', predictors: ['x'] }, 'cluster')], [])
  const base = { outcome: 'y', predictors: ['channel', 'price', 'month'], reference: { channel: 'shop', month: 'jan' }, interactions: [['price', 'channel'], ['price', 'month']] }
  assert.deepEqual(m.modelParams(LINEAR, base).interactions, [['price', 'channel'], ['price', 'month']])
  // a predictor taken away takes its reference and its interactions with it
  const less = m.modelSet(LINEAR, base, 'predictors', ['channel', 'price'])
  assert.deepEqual(less.reference, { channel: 'shop' })
  assert.deepEqual(less.interactions, [['price', 'channel']])
  assert.ok(!('reference' in m.modelSet(LINEAR, base, 'predictors', ['price', 'month']) && false))
  // twice, the same pair the other way round, a pair with itself, with a column that is no predictor: gone
  const odd = m.modelParams(LINEAR, { ...base, interactions: [['price', 'channel'], ['channel', 'price'], ['price', 'price'], ['price', 'zz'], ['price']] })
  assert.deepEqual(odd.interactions, [['price', 'channel']])
  // the event's value does not survive another column
  const km = m.modelSet(KM, { time: 't', event: 'e', event_value: '1' }, 'event', 'status')
  assert.ok(!('event_value' in km))
  assert.equal(m.modelSet(KM, km, 'event_value', 'dead').event_value, 'dead')
  assert.equal(m.modelSet(KM, { time: 't', event: 'e', event_value: '1' }, 'time', 'u').event_value, '1')
})

test('numbers are read as the boxes write them, and their problems are known', () => {
  assert.equal(m.modelNumber('0,5'), 0.5)
  assert.equal(m.modelNumber(' 3 '), 3)
  assert.ok(Number.isNaN(m.modelNumber('')) && Number.isNaN(m.modelNumber('x')))
  assert.deepEqual(m.modelNumbers('24, 12; 12 36'), [12, 24, 36])
  assert.deepEqual(m.modelNumbers([3, '1']), [1, 3])
  assert.deepEqual(m.modelParams(KM, { time: 't', event: 'e', event_value: '1', times: '24, 12' }).times, [12, 24])
  const f = (model, key) => m.modelFields(model).find((x) => x.key === key)
  assert.equal(m.modelNumberProblem(f(LINEAR, 'alpha'), 1), 'between')
  assert.equal(m.modelNumberProblem(f(LINEAR, 'alpha'), 0.05), '')
  assert.equal(m.modelNumberProblem(f(LINEAR, 'folds'), 2), 'range')
  assert.equal(m.modelNumberProblem(f(LINEAR, 'folds'), 10.5), 'whole')
  assert.equal(m.modelNumberProblem(f(KM, 'rmst_tau'), 0), 'above')
  assert.equal(m.modelNumberProblem(f(ARIMA, 'holdout'), -1), 'range')
  assert.equal(m.modelNumberProblem(f(ARIMA, 'horizon'), ''), '')
  // λ: a word or a number above 0
  assert.equal(m.modelParams(LINEAR, { outcome: 'y', predictors: ['x'], penalty: 'ridge', lambda: '0,3' }).lambda, 0.3)
  assert.equal(m.modelParams(LINEAR, { outcome: 'y', predictors: ['x'], penalty: 'ridge', lambda: 'cv_min' }).lambda, 'cv_min')
  assert.ok(!('lambda' in m.modelParams(LINEAR, { outcome: 'y', predictors: ['x'], penalty: 'ridge', lambda: 'nonsense' })))
})

test('the columns a model reads, what it says it does, and which columns can be what', () => {
  assert.deepEqual(m.modelColumns('linear', { outcome: 'y', predictors: ['x', 'z'], weights: 'w' }), ['y', 'x', 'z', 'w'])
  assert.deepEqual(m.modelColumns('arima', { time: 'day', value: 'qty', exog: ['price'] }), ['day', 'qty', 'price'])
  assert.equal(m.modelSummary('linear', { outcome: 'amount', predictors: ['price', 'channel'] }), 'amount ~ price + channel')
  assert.equal(m.modelSummary('linear', {}), '')
  assert.equal(m.modelSummary('kaplan_meier', { time: 't', event: 'e', strata: 'arm' }), 't, e ~ arm')
  assert.equal(m.modelSummary('cox', { time: 't', event: 'e', predictors: ['age'] }), 't, e ~ age')
  assert.equal(m.modelSummary('arima', { time: 'day', value: 'qty', p: 1, d: 1, q: 1, frequency: 'month' }), 'ARIMA(1,1,1) · qty')
  assert.equal(m.modelSummary('nope', {}), '')
  // numbers for a number, a time for a time, anything but a date for a predictor
  const by = (model, key) => m.modelFields(model).find((x) => x.key === key)
  assert.equal(m.modelColumnFits(by(LINEAR, 'outcome'), 'Float64'), true)
  assert.equal(m.modelColumnFits(by(LINEAR, 'outcome'), 'String'), false)
  assert.equal(m.modelColumnFits(by(LOGISTIC, 'outcome'), 'String'), true)
  assert.equal(m.modelColumnFits(by(LINEAR, 'predictors'), 'String'), true)
  assert.equal(m.modelColumnFits(by(LINEAR, 'predictors'), 'Date'), false)
  assert.equal(m.modelColumnFits(by(ARIMA, 'time'), 'Datetime(time_unit=\'us\', time_zone=None)'), true)
  assert.equal(m.modelColumnFits(by(ARIMA, 'time'), 'Int64'), true)
  // the boosting order: a date, an integer or a number (a float too), never a text
  const XGB = m.modelOf('xgboost')
  assert.equal(m.modelColumnFits(by(XGB, 'order_by'), 'Float64'), true)
  assert.equal(m.modelColumnFits(by(XGB, 'order_by'), 'Date'), true)
  assert.equal(m.modelColumnFits(by(XGB, 'order_by'), 'String'), false)
  assert.equal(m.modelColumnFits(by(ARIMA, 'time'), 'Float64'), false)
  assert.equal(m.modelColumnFits(by(ARIMA, 'exog'), 'Float64'), true)
  assert.equal(m.modelIsCategorical('String') && m.modelIsCategorical('Boolean'), true)
  assert.equal(m.modelIsCategorical('Int64') || m.modelIsCategorical('Date'), false)
})

test('the MLflow block holds nothing without a connection, and an experiment with one', () => {
  assert.equal(m.modelMlflow(null), undefined)
  assert.equal(m.modelMlflow({ experiment: 'x' }), undefined)
  assert.deepEqual(m.modelMlflow({ connectionId: 12 }), { connectionId: 12, experiment: '' })
  assert.equal(m.modelMlflowProblem({ connectionId: 12 }), 'experiment')
  assert.equal(m.modelMlflowProblem({ connectionId: 12, experiment: '  ' }), 'experiment')
  assert.equal(m.modelMlflowProblem(null), '')
  const block = m.modelMlflow({ connectionId: 12, experiment: ' Pasta/price elasticity ', registerAs: 'price-elasticity', continueWithout: true, junk: 1 })
  assert.deepEqual(block, { connectionId: 12, experiment: 'Pasta/price elasticity', registerAs: 'price-elasticity', continueWithout: true })
  assert.deepEqual(Object.keys(block).filter((k) => !m.MODEL_CATALOG.nodes.model.mlflow.includes(k)), [])
  assert.deepEqual(m.modelMlflow({ connectionId: 12, experiment: 'x', registerAs: ' ', continueWithout: false }), { connectionId: 12, experiment: 'x' })
})

test('Predict: the options of the model the version holds, and the columns it adds', () => {
  assert.deepEqual(m.predictFields('linear').map((f) => f.key), ['interval'])
  assert.deepEqual(m.predictFields('logistic').map((f) => f.key), ['threshold'])
  assert.deepEqual(m.predictFields('cox').map((f) => f.key), ['at_times'])
  assert.deepEqual(m.predictFields('arima').map((f) => f.key), ['interval', 'horizon'])
  assert.deepEqual(m.predictFields('kaplan_meier').map((f) => f.key), ['interval'])
  assert.deepEqual(m.predictFields(null), [])
  assert.deepEqual(m.predictParams('linear', { interval: true, threshold: 0.4, horizon: 3 }), { interval: true })
  assert.deepEqual(m.predictParams('linear', { interval: false }), {})
  assert.deepEqual(m.predictParams('cox', { at_times: '24, 12' }), { at_times: [12, 24] })
  assert.deepEqual(m.predictColumns('linear', {}), ['prediction'])
  assert.deepEqual(m.predictColumns('linear', { interval: true }), ['prediction', 'prediction_low', 'prediction_high'])
  assert.deepEqual(m.predictColumns('logistic', { threshold: 0.4 }), ['prediction', 'prediction_class'])
  assert.deepEqual(m.predictColumns('cox', { at_times: [12, 24] }), ['prediction', 'survival_at_12', 'survival_at_24'])
  assert.deepEqual(m.predictColumns('arima', { interval: true }), ['prediction', 'lower_80', 'upper_80', 'lower', 'upper'])
  assert.deepEqual(m.predictColumns('nope', {}), [])
  // the boosting models: an entry of the catalogue may be `model:variant`, the variant being the version's tag
  assert.deepEqual(m.predictFields('xgboost').map((f) => f.key), [])
  assert.deepEqual(m.predictFields('xgboost', undefined, 'regression').map((f) => f.key), [])
  assert.deepEqual(m.predictFields('xgboost', undefined, 'binary').map((f) => f.key), ['threshold'])
  assert.deepEqual(m.predictFields('catboost', undefined, 'binary').map((f) => f.key), ['threshold'])
  assert.deepEqual(m.predictFields('catboost', undefined, 'multiclass').map((f) => f.key), [])
  assert.deepEqual(m.predictFields('logistic', undefined, 'binary').map((f) => f.key), ['threshold'])
  assert.deepEqual(m.predictParams('xgboost', { threshold: 0.3 }, undefined, 'binary'), { threshold: 0.3 })
  assert.deepEqual(m.predictParams('xgboost', { threshold: 0.3 }, undefined, 'regression'), {})
  assert.deepEqual(m.predictColumns('xgboost', {}, undefined, 'regression'), ['prediction'])
  assert.deepEqual(m.predictColumns('catboost', {}, undefined, 'binary'), ['prediction', 'prediction_class'])
})

// the Predict form takes the variant of the version the node holds, not of the first one listed
test('the version a Predict node means decides its kind and its variant', () => {
  const list = [
    { model: 'churn', version: '1', kind: 'xgboost', variant: 'regression' },
    { model: 'churn', version: '2', kind: 'xgboost', variant: 'binary' },
    { model: 'churn', version: '10', kind: 'xgboost', variant: 'multiclass' },
    { model: 'other', version: '5', kind: 'logistic', variant: null },
  ]
  assert.equal(m.versionHeld(list, 'churn', '1').variant, 'regression') // a number: that one, not the newest
  assert.equal(m.versionHeld(list, 'churn', '2').variant, 'binary')
  assert.equal(m.versionHeld(list, 'churn', 'latest').version, '10') // 10 is newer than 2 (a number, not a text)
  assert.equal(m.versionHeld(list.slice().reverse(), 'churn', 'latest').variant, 'multiclass') // whatever the order
  assert.equal(m.versionHeld(list, 'churn', '7'), null)
  assert.equal(m.versionHeld(list, 'nope', 'latest'), null)
  // v1 regression, v2 binary, the node on v1: no threshold; on v2: the threshold
  const keys = (version) => m.predictFields('xgboost', undefined, m.versionHeld(list, 'churn', version)?.variant).map((f) => f.key)
  assert.deepEqual(keys('1'), [])
  assert.deepEqual(keys('2'), ['threshold'])
  // and the form reads it from there
  assert.match(read('../components/ui/PredictForm.vue'), /versionHeld\(versions\.value, props\.data\?\.registeredModel, props\.data\?\.version\)/)
})

// ── what is drawn ────────────────────────────────────────────────────────────

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

const Select = defineComponent({
  name: 'Select',
  inheritAttrs: false,
  props: ['modelValue', 'options', 'placeholder', 'searchable'],
  setup: (props, { attrs }) => () => h('div', { class: 'select', 'data-value': String(props.modelValue ?? ''), ...attrs },
    (props.options ?? []).map((o) => h('span', { class: 'option', 'data-hint': o.hint }, o.label ?? String(o.value)))),
})

async function render (comp, props) {
  const app = createSSRApp({ render: () => h(comp, props) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en: MESSAGES.en }, missingWarn: false, fallbackWarn: false }))
  app.component('Select', Select)
  const warnings = []
  app.config.warnHandler = (w) => warnings.push(w)
  app.config.errorHandler = (e) => { throw e }
  const html = await renderToString(app)
  assert.deepEqual(warnings, [])
  return html
}
const text = (html) => html.replace(/<!--.*?-->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').trim()
const count = (html, piece) => html.split(piece).length - 1
const selectsOf = (html) => [...html.matchAll(/<div class="select"[^>]*>(.*?)<\/div>/g)].map((x) => text(x[1]))

const COLUMNS = [
  { name: 'amount', dtype: 'Float64' }, { name: 'channel', dtype: 'String' }, { name: 'qty', dtype: 'Int64' },
  { name: 'day', dtype: 'Date' }, { name: 'vip', dtype: 'Boolean' },
]
const node = (model, params = {}, more = {}) => ({ nodeId: 'ctl-1', data: { model, params, timeoutSeconds: 900, retries: 0, ...more }, inputColumns: COLUMNS })

test('a new linear regression: its fields from the catalogue with their words, what it lacks said once', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const html = await render(Form, node('linear', m.modelDefaults('linear')))
  const page = text(html)
  for (const words of ['Explains a number with other columns', 'Outcome (a number)', 'Predictors', 'Estimation', 'Penalty', 'Reference levels', 'Intercept',
    'Weights column', 'Standard errors', 'Order of the rows', 'Significance level', 'Seed', 'Output',
    'Ordinary least squares', 'Weighted least squares', 'No penalty', 'Ridge', 'Lasso', 'Elastic net', 'Still to set: Outcome (a number), Predictors.']) {
    assert.ok(page.includes(words), `«${words}» is not on the page: ${page}`)
  }
  // only what applies to the unpenalized variant
  for (const gone of ['Penalty strength', 'Cross-validation folds', 'Lasso share', 'Error measure', 'Standardize the predictors', 'Cluster column']) assert.ok(!page.includes(gone), gone)
  // in the catalogue's order
  const order = ['Outcome (a number)', 'Predictors', 'Estimation', 'Penalty', 'Intercept', 'Weights column', 'Standard errors', 'Order of the rows', 'Significance level', 'Seed', 'Output']
  assert.deepEqual([...order].sort((a, b) => page.indexOf(a) - page.indexOf(b)), order)
  // the outcome: numbers only; the predictors: boxes, no date; the weights and the order: any column that is left
  const selects = selectsOf(html)
  assert.equal(selects[0], 'amount qty')
  assert.match(html, /<input type="checkbox"[^>]*>\s*<span>\s*channel/)
  assert.ok(!/<span>\s*day\s*<\/span>/.test(html.slice(html.indexOf('Predictors'), html.indexOf('Estimation'))), 'a date is no predictor')
  assert.equal(count(html, 'role="switch"'), 1)
  // the variants as buttons, the arrows move among them; a choice of many is a list
  assert.match(html, /<input type="radio" name="model-ctl-1-penalty"[^>]*value="none"[^>]*checked/)
  assert.match(html, /<input type="radio" name="model-ctl-1-estimation"[^>]*value="ols"[^>]*checked/)
  assert.ok(selects.includes('Classical Robust (HC0) Robust (HC1) Robust (HC2) Robust (HC3) Robust to autocorrelation (HAC) Clustered'))
  // the MLflow block and the run's limits
  assert.ok(page.includes('MLflow connection') && page.includes('No MLflow: the results stay with the run') && page.includes('Longest run (seconds)'))
  assert.ok(page.includes('No MLflow connection is available to you'))
  assert.match(html, /type="number" min="10" max="86400" value="900"/)
  // every field is named for a screen reader
  assert.ok(html.includes('<label for="model-ctl-1-alpha"') && html.includes('id="model-ctl-1-alpha"'))
  assert.ok(html.includes('aria-labelledby="model-ctl-1-outcome-label"'))
  assert.ok(html.includes('<legend') && html.includes('<fieldset'))
})

test('with a penalty the form is the penalized variant\'s: λ, folds, the share of lasso; no standard errors', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const base = { outcome: 'amount', predictors: ['qty', 'channel'] }
  let page = text(await render(Form, node('linear', { ...base, penalty: 'ridge' })))
  for (const words of ['Penalty strength (λ)', 'Cross-validation folds', 'Keep together by', 'Error measure', 'Standardize the predictors',
    'Largest within one error of the best', 'The one with the smallest error', 'A value']) assert.ok(page.includes(words), words)
  for (const gone of ['Standard errors', 'Order of the rows', 'Estimation', 'Lasso share']) assert.ok(!page.includes(gone), gone)
  page = text(await render(Form, node('linear', { ...base, penalty: 'elastic_net' })))
  assert.ok(page.includes('Lasso share') && page.includes('Choose by cross-validation'))
  // clustered errors ask for the column
  page = text(await render(Form, node('linear', { ...base, se: 'cluster' })))
  assert.ok(page.includes('Cluster column'))
  assert.ok(page.includes('Clustered standard errors need the column of the clusters.'), page)
  assert.ok(page.includes('Still to set: Cluster column.'))
  // WLS: the weights are asked for
  page = text(await render(Form, node('linear', { ...base, estimation: 'wls' })))
  assert.ok(page.includes('Weighted least squares need the column of the weights.') && page.includes('Still to set: Weights column.'))
})

test('a set node shows what it has; a column that went, an outcome that is also a predictor, a missing experiment are said', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const params = { outcome: 'gone', predictors: ['qty', 'amount'], weights: 'qty', interactions: [['qty', 'amount']] }
  const html = await render(Form, node('linear', params, { mlflow: { connectionId: 12, experiment: '' } }))
  const page = text(html)
  assert.match(html, /class="select" data-value="gone"/)
  assert.match(html, /<span class="option" data-hint="not in the step&#39;s input">gone<\/span>/)
  assert.ok(page.includes('The column «qty» is both «Predictors / Weights column»: choose another.'), page)
  // an interaction of two predictors: a row of two lists and a button to take it away
  assert.ok(html.includes('aria-label="Interactions 1: first predictor"') && html.includes('aria-label="Remove this interaction 1"'))
  assert.ok(page.includes('Add an interaction'))
  // the MLflow block: connection 12 is not one the person has: listed, with that said; the experiment is needed
  assert.ok(page.includes('Connection #12 (not available to you)'))
  assert.ok(page.includes('Give the experiment a name.'))
  assert.match(html, /id="model-ctl-1-mlflow-experiment"[^>]*aria-invalid="true"/)
  assert.ok(page.includes('Register the model as') && page.includes('Continue without MLflow'))
})

test('the MLflow connections offered are the person\'s `mlflow` ones, and the node says which it has', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const connections = [{ id: 1, name: 'Warehouse', db_type: 'postgres' }, { id: 12, name: 'Tracking', db_type: 'mlflow' }, { id: 13, name: 'Other tracking', db_type: 'mlflow' }]
  const html = await render(Form, { ...node('linear', {}, { mlflow: { connectionId: 12, experiment: 'Pasta/price elasticity', registerAs: 'price-elasticity', continueWithout: true } }), connections })
  const selects = selectsOf(html)
  assert.ok(selects.includes('No MLflow: the results stay with the run Tracking Other tracking'), selects.join(' | '))
  assert.ok(!text(html).includes('Warehouse'))
  assert.match(html, /id="model-ctl-1-mlflow-experiment"[^>]*value="Pasta\/price elasticity"/)
  assert.match(html, /value="price-elasticity"/)
  assert.match(html, /role="switch"[^>]*checked/)
  assert.ok(!text(html).includes('Give the experiment a name.'))
  // with no connection chosen: no experiment to fill
  const without = await render(Form, { ...node('linear', {}), connections })
  assert.ok(!text(without).includes('Register the model as') && text(without).includes('Log each run of the flow to an MLflow server'))
})

test('a logistic regression: the event\'s value is asked at the outcome; Firth and the profile intervals say why they are off', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const asked = []
  const fetchDistinct = (column) => { asked.push(column); return Promise.resolve(['yes', 'no', null]) }
  let html = await render(Form, { ...node('logistic', { outcome: 'vip', event_value: 'true', predictors: ['amount'], link: 'probit' }), fetchDistinct })
  assert.deepEqual(asked, ['vip'])
  assert.match(html, /<input id="model-ctl-1-event_value" type="text" autocomplete="off" list="model-ctl-1-event_value-values" value="true"/)
  assert.ok(html.includes('<datalist id="model-ctl-1-event_value-values"'))
  // the outcome may be any column that is not a predictor already
  assert.equal(selectsOf(html)[0], 'channel qty day vip')
  assert.match(html, /<input type="radio"[^>]*value="firth"[^>]*disabled/)
  assert.ok(text(html).includes('Firth\'s correction is for the logit link'))
  html = await render(Form, node('logistic', { outcome: 'vip', predictors: ['amount'], se: 'hc3', ci: 'wald' }))
  assert.match(html, /<input type="radio"[^>]*value="profile"[^>]*disabled/)
  assert.ok(text(html).includes('Profile likelihood intervals go with classical standard errors'))
  assert.ok(!/value="wald"[^>]*disabled/.test(html))
  // the event's value waits for its column
  const waiting = await render(Form, node('logistic', {}))
  assert.ok(text(waiting).includes('Choose «Outcome» first.') || /disabled/.test(waiting))
  assert.match(waiting, /id="model-ctl-1-event_value"[^>]*disabled/)
})

test('the reference level of each categorical predictor is chosen among its values', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const asked = []
  const fetchDistinct = (column) => { asked.push(column); return Promise.resolve(['web', 'shop', 'phone']) }
  const html = await render(Form, { ...node('linear', { outcome: 'amount', predictors: ['qty', 'channel'], reference: { channel: 'shop' } }), fetchDistinct })
  assert.deepEqual(asked, ['channel']) // qty is a number: no reference
  assert.ok(text(html).includes('Reference levels') && text(html).includes('channel'))
})

test('Kaplan–Meier: the test and the pairs show with strata only; the times are a list of numbers', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const base = { time: 'amount', event: 'vip', event_value: 'true' }
  let page = text(await render(Form, node('kaplan_meier', base)))
  for (const words of ['Follow-up time (a number)', 'Event column', 'Value that means the event happened', 'Strata column', 'Confidence band', 'Survival at times', 'Restricted mean up to']) {
    assert.ok(page.includes(words), words)
  }
  assert.ok(!page.includes('Test between strata') && !page.includes('Compare each pair of strata'))
  const html = await render(Form, node('kaplan_meier', { ...base, strata: 'channel', times: [12, 24] }))
  page = text(html)
  assert.ok(page.includes('Test between strata') && page.includes('Compare each pair of strata') && page.includes('Peto–Peto'))
  assert.match(html, /id="model-ctl-1-times"[^>]*value="12, 24"/)
  assert.ok(page.includes('Up to 10 times'))
})

test('ARIMA: the orders by hand, or their maxima for the automatic search; the title is the node\'s', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  const base = { time: 'day', value: 'amount', frequency: 'month' }
  let html = await render(Form, node('arima', { ...base, ...m.modelNodeData('arima', 'sarima').params }))
  let page = text(html)
  for (const words of ['AR order (p)', 'Differences (d)', 'MA order (q)', 'Seasonal AR order (P)', 'Seasonal differences (D)', 'Seasonal MA order (Q)',
    'Seasonal period', 'Exogenous columns', 'Forecast steps', 'Rows kept out to test the forecast', 'Monthly']) assert.ok(page.includes(words), words)
  assert.ok(!page.includes('Largest p') && !page.includes('Criterion'))
  // the seasonal period's box shows the frequency's own
  assert.match(html, /id="model-ctl-1-s"[^>]*placeholder="12"/)
  // the time column: a date, a datetime or a whole number
  assert.equal(selectsOf(html)[0], 'qty day')
  html = await render(Form, node('arima', { ...base, orders: 'auto', exog: ['qty'] }))
  page = text(html)
  for (const words of ['Largest p', 'Largest D', 'Criterion', 'Stepwise search', 'Test for differences', 'AICc']) assert.ok(page.includes(words), words)
  assert.ok(!page.includes('AR order (p)') && !page.includes('Forecast steps'))
  // a model with nothing yet: the frequency is the one thing it asks for
  page = text(await render(Form, node('arima', { time: 'day', value: 'amount' })))
  assert.ok(page.includes('Still to set: Frequency of the series.'))
})

test('a group that is off says so on the form, the keys do not change; an observer cannot edit', async () => {
  const Form = await component('components/ui/ModelForm.vue')
  assert.ok(text(await render(Form, { ...node('linear', {}), off: 'not_enabled' })).includes('The model group «Regression» is not enabled'))
  assert.ok(text(await render(Form, { ...node('linear', {}), off: 'kubernetes' })).includes('Needs Kubernetes'))
  assert.ok(text(await render(Form, { ...node('linear', {}), off: 'not_allowed' })).includes('You may not use this model group'))
  assert.ok(!text(await render(Form, { ...node('linear', {}), off: '' })).includes('model group'))
  assert.match(await render(Form, { ...node('linear', {}), readOnly: true }), /<fieldset class="modelform" disabled/)
  // the timeout is at most the group's
  assert.ok(text(await render(Form, { ...node('linear', {}), maxTimeout: 3600 })).includes('At most 3600 seconds for this group.'))
  // columns being resolved, or none
  assert.ok((await render(Form, { ...node('linear', {}), inputColumns: [], columnsLoading: true })).includes('aria-busy="true"'))
  assert.ok(text(await render(Form, { ...node('linear', {}), inputColumns: [] })).includes(MESSAGES.en.paramForm.noUpstreamColumns))
})

test('the Predict form: the connection, the experiment read from the server, the version, the options of its model', async () => {
  const Form = await component('components/ui/PredictForm.vue')
  const connections = [{ id: 12, name: 'Tracking', db_type: 'mlflow' }, { id: 1, name: 'Warehouse', db_type: 'postgres' }]
  const data = { connectionId: 12, experiment: 'Pasta/price elasticity', registeredModel: 'price-elasticity', version: '7', params: { interval: true }, timeoutSeconds: 900, retries: 0 }
  const html = await render(Form, { nodeId: 'ctl-2', data, inputColumns: COLUMNS, connections })
  const page = text(html)
  assert.ok(page.includes('Applies a model registered in MLflow') && page.includes('MLflow connection') && page.includes('Experiment'))
  assert.ok(selectsOf(html).includes('Tracking'))
  assert.ok(!page.includes('Warehouse'))
  // the server cannot be asked here (nothing answers): the experiment is written, and it is said
  assert.ok(html.includes('aria-busy="true"') || page.includes('could not be read'))
  assert.ok(page.includes('Longest run (seconds)'))
  // a prediction with no connection: only the connection
  const bare = text(await render(Form, { nodeId: 'ctl-2', data: { params: {} }, inputColumns: COLUMNS, connections }))
  assert.ok(bare.includes('MLflow connection') && !bare.includes('Experiment') && !bare.includes('Model version'))
  assert.ok(text(await render(Form, { nodeId: 'ctl-2', data: { params: {} }, inputColumns: COLUMNS, connections: [] })).includes('No MLflow connection is available to you'))
})

test('the palette: the Models group after Statistics, closed, by group, with Predict; greyed out on Compose; nothing when none is enabled', async () => {
  const Sidebar = await component('components/ui/OpSidebar.vue')
  const { useModelGroups } = await import('../composables/useModelGroups.ts')
  const groups = useModelGroups()
  const operations = ['cast', 'filter', 'foreach', 'pivot', 'stat_t_two_sample']
  // the route has not answered, or nobody has a group: no Models group
  groups.state.value = null
  assert.ok(!text(await render(Sidebar, { operations, mlflowConnections: 1 })).includes('Models'))
  groups.state.value = { disabled_reason: null, groups: [group('regression', false)] }
  assert.ok(!text(await render(Sidebar, { operations, mlflowConnections: 1 })).includes('Models'))
  // two groups turned on
  groups.state.value = STATE
  let html = await render(Sidebar, { operations, mlflowConnections: 1 })
  let page = text(html)
  const order = ['Sources', 'Transforms', 'Statistics', 'Models', 'Control', 'Output']
  assert.deepEqual([...order].sort((a, b) => page.indexOf(a) - page.indexOf(b)), order)
  assert.match(html, /<button type="button" class="group-title group-toggle"[^>]*aria-expanded="false"[^>]*aria-controls="palette-models"/)
  assert.match(html, /id="palette-models"[^>]*style="display:none;"/)
  const inside = text('<' + html.slice(html.indexOf('id="palette-models"'), html.indexOf('Control')))
  assert.ok(inside.includes('Predict') && inside.includes('Regression Linear regression Logistic regression'), inside)
  assert.ok(inside.includes('Time series ARIMA AR MA ARMA ARIMA (p, d, q) SARIMA SARIMAX'), inside)
  assert.ok(!inside.includes('Survival') && !inside.includes('Kaplan'))
  // 1 Predict + 2 + 7 entries
  assert.match(html, /class="group-count"[^>]*>10</)
  // each entry is draggable and reached with Tab, Enter and Space add it
  assert.match(html, /class="item"[^>]*draggable="true"[^>]*role="button"[^>]*tabindex="0"[^>]*title="Explains a number with other columns/)
  // without an MLflow connection: no Predict
  assert.ok(!text(await render(Sidebar, { operations, mlflowConnections: 0 })).includes('Predict'))
  // an observer has no models
  assert.ok(!text(await render(Sidebar, { operations, mlflowConnections: 1, readOnly: true })).includes('Models'))
  // Compose: all the groups, greyed out, not draggable, with the reason
  groups.state.value = { disabled_reason: 'kubernetes', groups: [] }
  html = await render(Sidebar, { operations, mlflowConnections: 1 })
  page = text(html)
  assert.ok(page.includes('Survival') && page.includes('Kaplan–Meier'))
  assert.match(html, /class="item off"[^>]*draggable="false"[^>]*aria-disabled="true"/)
  assert.ok(html.includes('title="Needs Kubernetes'))
  // a search opens the group by itself
  groups.state.value = STATE
  html = await render(Sidebar, { operations, mlflowConnections: 1 })
  assert.ok(!text(html).includes('Kaplan'))
  groups.state.value = null
})

// ── the check is idempotent: what it keeps, given back, is kept as it is ─────
// The same test is in the engine (backend/tests/test_models_catalog.py), the gateway
// (internal/modelcatalog) and the assistant (gateway/tests/test_ai_flow_agent.py).

const twice = (what, model, params) => {
  const first = m.modelParams(model, params)
  const second = m.modelParams(model, first)
  assert.equal(JSON.stringify(second), JSON.stringify(first), `${what}: ${model.id}: checking twice changes the answer`)
}

test('modelParams twice is modelParams once: the defaults, every preset, every choice of every variant', () => {
  for (const model of m.MODEL_CATALOG.models) {
    twice('the defaults', model, m.modelDefaults(model.id))
    for (const preset of Object.keys(model.presets ?? {})) twice(`preset ${preset}`, model, m.modelDefaults(model.id, preset))
    const base = m.modelDefaults(model.id)
    const picks = []
    for (const f of m.modelFields(model)) {
      if (f.control === 'choice') for (const c of f.choices) picks.push([f.key, c])
      if (f.control === 'boolean') picks.push([f.key, true], [f.key, false])
    }
    picks.forEach((a, i) => {
      twice(`${a[0]}=${a[1]}`, model, { ...base, [a[0]]: a[1] })
      for (const b of picks.slice(i + 1)) if (a[0] !== b[0]) twice(`${a} ${b}`, model, { ...base, [a[0]]: a[1], [b[0]]: b[1] })
    })
  }
})

test('modelParams twice is modelParams once on the cases the engine and the gateway share', () => {
  const cases = JSON.parse(read('../../backend/tests/fixtures/models/params_cases.json')).cases
  assert.ok(cases.length >= 40, `${cases.length} cases`)
  for (const c of cases) {
    const model = m.modelOf(c.model)
    if (!model) continue // «there is no model»: nothing to keep
    twice(c.what, model, c.params)
    if (c.checked) twice(`${c.what} (its answer)`, model, c.checked)
  }
})

test('an empty list of columns is not given, once or twice (an empty exog is none)', () => {
  const first = m.modelParams(ARIMA, { time: 't', value: 'v', frequency: 'month', exog: [] })
  assert.ok(!('exog' in first))
  assert.deepEqual(m.modelParams(ARIMA, first), first)
})
