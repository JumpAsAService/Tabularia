// The node «Bayesian model» without a browser (docs/specs/bayes-flow-node.md, lot F3): the pure rules the panel and the canvas
// node draw (composables/bayesNode.ts) — the data of a new node and its seed, the columns each kind of variable takes, the move
// to a newer version (which bindings it keeps, which it clears, which variables it names unbound), the notice «a newer version
// exists» (a layout-only version says nothing), what the pinned version can check, the initial values, the sampler's ranges — and
// the places the node is wired into (the panel's branch, the canvas node's template, the palette's item, the five languages).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const N = await import('~/composables/bayesNode.ts')
const { bayesOpenTemplate } = await import('~/composables/bayesTemplates.ts')

const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const read = (p) => readFileSync(join(FRONTEND, p), 'utf8')
const LANGS = ['en', 'it', 'de', 'es', 'fr']

const vars = (id) => N.bayesNodeVariables(bayesOpenTemplate(id))
const byNode = (list, node) => list.find((v) => v.node === node)

test('a new node: the sampler\'s defaults, the summary, a timeout fit for sampling and a seed of its own', () => {
  const d = N.bayesNodeData(1840277361)
  assert.deepEqual(d.sampler, { chains: 4, warmup: 1000, draws: 1000, seed: 1840277361, adapt_delta: 0.8, max_treedepth: 10, init: null })
  assert.equal(d.output, 'summary')
  assert.equal(d.timeoutSeconds, 1800)
  assert.deepEqual(d.bindings, {})
  assert.equal(d.bayesModelId, null)
  assert.equal(d.bayesModelVersion, null)
  assert.equal(N.bayesNodeSeed(() => 0), 1)
  assert.equal(N.bayesNodeSeed(() => 0.999999999999), 2_147_483_647)
  const seeds = new Set(Array.from({ length: 50 }, () => N.bayesNodeData().sampler.seed))
  assert.ok(seeds.size > 45, 'two nodes do not share a stream by accident')
  for (const s of seeds) assert.ok(Number.isInteger(s) && s >= 1 && s <= 2_147_483_647)
})

test('the variables of a pinned version: the data nodes, by the mirror', () => {
  assert.deepEqual(vars('hier_slopes').map((v) => [v.node, v.type]), [['g', 'group_index'], ['x', 'real'], ['y', 'real']])
  assert.deepEqual(vars('glm_logistic').map((v) => [v.node, v.type, v.support]), [['x', 'real', ''], ['y', 'int', 'binary']])
  assert.deepEqual(vars('ts_ar').map((v) => [v.node, v.type]), [['t', 'time'], ['y', 'real']])
  // the predictor's standardization is shown, the outcome's is not; the outcome is the one the likelihood observes
  assert.equal(byNode(vars('glm_linear'), 'x').standardize, true)
  assert.equal(byNode(vars('glm_linear'), 'y').observed, true)
  assert.deepEqual(N.bayesNodeVariables(null), [])
})

test('the columns a type takes: numbers, integers, text for a group or a category, dates for the time', () => {
  const columns = [
    { name: 'score', dtype: 'Float64' }, { name: 'n', dtype: 'Int64' }, { name: 'school', dtype: 'String' }, { name: 'flag', dtype: 'Boolean' },
    { name: 'day', dtype: 'Date' }, { name: 'at', dtype: "Datetime(time_unit='us', time_zone=None)" }, { name: 'tag', dtype: 'Categorical' },
  ]
  const names = (v) => N.bayesNodeColumnsFor(v, columns).map((c) => c.name)
  assert.deepEqual(names({ type: 'real', support: '' }), ['score', 'n'])
  assert.deepEqual(names({ type: 'int', support: 'nonnegative' }), ['score', 'n'])
  assert.deepEqual(names({ type: 'int', support: 'binary' }), ['score', 'n', 'flag'], '0 or 1 takes a boolean too')
  assert.deepEqual(names({ type: 'group_index', support: '' }), ['n', 'school', 'tag'])
  assert.deepEqual(names({ type: 'category', support: '' }), ['n', 'school', 'tag'])
  assert.deepEqual(names({ type: 'time', support: '' }), ['score', 'n', 'day', 'at'])
  assert.equal(N.bayesNodeFits({ type: 'real', support: '' }, 'String'), false)
  assert.equal(N.bayesNodeFits({ type: 'time', support: '' }, 'Date'), true)
})

test('the unbound variables are named, by label, else by name, else by id', () => {
  const list = vars('hier_slopes')
  assert.deepEqual(N.bayesNodeUnbound(list, { g: 'school', y: '  ' }).map((v) => v.node), ['x', 'y'])
  assert.deepEqual(N.bayesNodeUnbound(list, { g: 'a', x: 'b', y: 'c' }), [])
  assert.equal(N.bayesNodeNameOf({ node: 'n1', name: 'hours', label: 'Hours of study' }), 'Hours of study')
  assert.equal(N.bayesNodeNameOf({ node: 'n1', name: 'hours', label: '' }), 'hours')
  assert.equal(N.bayesNodeNameOf({ node: 'n1', name: '', label: '' }), 'n1')
})

test('«Use version N»: the bindings of the data nodes still there stay when their column still fits; the others are cleared and named', () => {
  const columns = [{ name: 'score', dtype: 'Float64' }, { name: 'school', dtype: 'String' }, { name: 'hours', dtype: 'Float64' }]
  const old = { g: 'school', x: 'hours', y: 'score', gone: 'score' }
  // the newer version keeps g, x and y, drops «gone» and adds the time t
  const next = [...vars('hier_slopes'), { node: 't', name: 't', label: 'Week', type: 'time', support: '' }]
  const move = N.bayesNodeMove(old, next, columns)
  assert.deepEqual(move.bindings, { g: 'school', x: 'hours', y: 'score' })
  assert.deepEqual(move.kept.sort(), ['g', 'x', 'y'])
  assert.deepEqual(move.cleared, [])
  assert.deepEqual(move.unbound.map((v) => v.node), ['t'], 'the variable the newer version adds is listed unbound')
  // a variable whose type changed: its column does not fit any more
  const changed = vars('hier_slopes').map((v) => (v.node === 'x' ? { ...v, type: 'group_index' } : v))
  const second = N.bayesNodeMove(old, changed, [{ ...columns[2], dtype: 'Float64' }, columns[0], columns[1]])
  assert.deepEqual(second.cleared.map((v) => v.node), ['x'])
  assert.equal('x' in second.bindings, false)
  assert.deepEqual(second.unbound.map((v) => v.node), ['x'])
  // a column the input no longer has is kept (the panel shows it struck); an input not read yet keeps everything
  assert.deepEqual(N.bayesNodeMove(old, vars('hier_slopes'), [columns[0]]).bindings, { g: 'school', x: 'hours', y: 'score' })
  assert.deepEqual(N.bayesNodeMove(old, vars('hier_slopes'), []).bindings, { g: 'school', x: 'hours', y: 'score' })
  assert.deepEqual(N.bayesNodeMove(null, vars('hier_slopes'), columns).unbound.length, 3)
})

test('the notice: a later version that changes the model; a layout-only version says nothing', () => {
  const versions = [{ version: 1, semantic_hash: 'a' }, { version: 2, semantic_hash: 'a' }, { version: 3, semantic_hash: 'b' }]
  assert.equal(N.bayesNodeNewer(3, 3, versions), null, 'up to date')
  assert.deepEqual(N.bayesNodeNewer(3, 2, versions), { version: 3, changed: true, pinnedMissing: false })
  assert.equal(N.bayesNodeNotice(N.bayesNodeNewer(3, 2, versions)), true)
  const layout = N.bayesNodeNewer(2, 1, versions)
  assert.deepEqual(layout, { version: 2, changed: false, pinnedMissing: false })
  assert.equal(N.bayesNodeNotice(layout), false)
  assert.equal(N.bayesNodeNotice(null), false)
  assert.equal(N.bayesNodeNewer(4, 1, versions), null, 'a version the list does not hold: nothing to compare')
  assert.deepEqual(N.bayesNodeNewer(3, 9, versions.slice(1)), null, 'pinned later than current')
  assert.deepEqual(N.bayesNodeNewer(3, 1, versions.slice(1)), { version: 3, changed: true, pinnedMissing: true })
})

test('what a version can check, from its stored data manifest', () => {
  const entry = (kind) => ({ stan: kind, kind })
  assert.equal(N.bayesNodeChecks([entry('size'), entry('real')]), 'none')
  assert.equal(N.bayesNodeChecks([entry('size'), entry('checks_on'), entry('prior_count')]), 'both')
  assert.equal(N.bayesNodeChecks([entry('checks_on'), entry('count')]), 'posterior')
  assert.equal(N.bayesNodeChecks({ data: [entry('checks_on')] }), 'posterior')
  assert.equal(N.bayesNodeChecks(null), 'none')
})

test('the initial values: a radius, or a value for each single-number parameter; none left is null', () => {
  const quantities = [
    { stan: 'mu_a', role: 'quantity', node: 'mua', dims: [] },
    { stan: 'sigma', role: 'quantity', node: 'sigma', dims: [] },
    { stan: 'a', role: 'quantity', node: 'a', dims: [{ plate: 'grp' }] },
    { stan: 'Z_eff', role: 'internal', dims: [] },
    { stan: 'mu_line', role: 'quantity', node: 'line', dims: [] },
  ]
  const targets = N.bayesNodeInitTargets(quantities, new Set(['mua', 'sigma', 'a']))
  assert.deepEqual(targets, [{ stan: 'mu_a', node: 'mua' }, { stan: 'sigma', node: 'sigma' }], 'no vector, no internal, no node that is not a parameter')
  assert.equal(N.bayesNodeInitMode(null), 'default')
  assert.equal(N.bayesNodeInitMode(0.5), 'radius')
  assert.equal(N.bayesNodeInitMode({ sigma: 1 }), 'values')
  let init = N.bayesNodeInitWith(null, 'sigma', '1.5')
  assert.deepEqual(init, { sigma: 1.5 })
  init = N.bayesNodeInitWith(init, 'mu_a', '-2')
  assert.deepEqual(init, { sigma: 1.5, mu_a: -2 })
  assert.deepEqual(N.bayesNodeInitWith(init, 'sigma', ''), { mu_a: -2 })
  assert.equal(N.bayesNodeInitWith({ sigma: 1 }, 'sigma', 'abc'), null)
  // the pin moves: the values of a parameter that is gone go; a radius stays
  assert.deepEqual(N.bayesNodeInitKeep({ sigma: 1, tau: 2 }, [{ stan: 'sigma' }]), { sigma: 1 })
  assert.equal(N.bayesNodeInitKeep({ tau: 2 }, [{ stan: 'sigma' }]), null)
  assert.equal(N.bayesNodeInitKeep(2, []), 2)
  assert.equal(N.bayesNodeInitKeep(null, [{ stan: 'sigma' }]), null)
})

test('the fields keep to the contract\'s ranges', () => {
  assert.equal(N.bayesNodeWhole('20', 1, 8, 4), 8)
  assert.equal(N.bayesNodeWhole('0', 1, 8, 4), 1)
  assert.equal(N.bayesNodeWhole('3.6', 1, 8, 4), 4)
  assert.equal(N.bayesNodeWhole('', 1, 8, 4), 4)
  assert.equal(N.bayesNodeWhole('x', 0, 10000, 1000), 1000)
  assert.equal(N.bayesNodeDelta('0.95', 0.8), 0.95)
  assert.equal(N.bayesNodeDelta('1', 0.8), 0.999, 'strictly between 0 and 1')
  assert.equal(N.bayesNodeDelta('0', 0.8), 0.001)
  assert.equal(N.bayesNodeDelta('', 0.8), 0.8)
  assert.equal(N.bayesNodeReplicated(undefined, 1000), 50)
  assert.equal(N.bayesNodeReplicated(undefined, 20), 20, 'at most the draws')
  assert.equal(N.bayesNodeReplicated(5000, 1000), 1000)
  assert.deepEqual(N.bayesNodeSamplerOf(undefined, 7), { chains: 4, warmup: 1000, draws: 1000, seed: 7, adapt_delta: 0.8, max_treedepth: 10, init: null })
  assert.equal(N.bayesNodeSamplerOf({ chains: 2, init: 0.5 }).init, 0.5)
})

test('the node is wired into the editor: the panel, the canvas node, the palette, the title', () => {
  assert.match(read('components/ui/NodePanel.vue'), /node\.type === 'bayes_model'[\s\S]{0,80}<BayesNodePanel/)
  const editor = read('components/FlowEditor.vue')
  assert.match(editor, /<template #node-bayes_model="props">\s*<BayesModelNode/)
  assert.ok(editor.includes("if (kind === 'bayes_model') return { type: 'bayes_model', data: bayesNodeData() }"), 'the seed is drawn when the node is made')
  assert.ok(editor.includes("n.type === 'bayes_model' && !given"))
  const palette = read('components/ui/OpSidebar.vue')
  assert.ok(palette.includes("emit('add', 'bayes_model')") && palette.includes("onDragStart($event, 'bayes_model')"))
  assert.ok(palette.includes('bayesUsable(modelGroups.state.value)'))
  const panel = read('components/bayes/BayesNodePanel.vue')
  // the panel writes the model and its version together
  assert.match(panel, /emit\('update', \{ bayesModelId: m\.id, bayesModelVersion: m\.current_version,/)
  assert.match(panel, /bayesModelId: modelId\.value, bayesModelVersion: v\.version,/)
})

test('every word of the node is in the five languages, with the same placeholders, and none carries a brace, a bar or an at sign', async () => {
  const words = {}
  for (const lang of LANGS) words[lang] = (await import(`../i18n/locales/${lang}.gen.ts`)).default.bayesNode
  const flat = (o, p = '', out = {}) => { for (const [k, v] of Object.entries(o)) typeof v === 'string' ? (out[p + k] = v) : flat(v, `${p}${k}.`, out); return out }
  const en = flat(words.en)
  assert.ok(Object.keys(en).length > 80)
  const holders = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
  for (const lang of LANGS) {
    const own = flat(words[lang])
    assert.deepEqual(Object.keys(own), Object.keys(en), lang)
    for (const [key, text] of Object.entries(own)) {
      assert.deepEqual(holders(text), holders(en[key]), `${lang} ${key}`)
      assert.ok(!/[|@]/.test(text.replace(/\{\w+\}/g, '')) && !/[{}]/.test(text.replace(/\{\w+\}/g, '')), `${lang} ${key}`)
    }
  }
  // every key the three components name exists
  const named = new Set()
  for (const file of ['components/bayes/BayesNodePanel.vue', 'components/nodes/BayesModelNode.vue']) {
    for (const m of read(file).matchAll(/['"`]bayesNode\.([\w.]+)['"`]/g)) named.add(m[1])
  }
  for (const key of named) assert.ok(key in en, key)
  assert.ok(named.size > 40)
})

test('the panel writes a missing seed and a missing timeout when it opens a node', () => {
  const panel = read('components/bayes/BayesNodePanel.vue')
  assert.match(panel, /seed: bayesNodeSeed\(\) \}/)
  assert.ok(panel.includes('patch.timeoutSeconds = BAYES_NODE_TIMEOUT.default'))
})

// ── the prior predictive check (docs/specs/bayes-flow-node.md, lot F4) ─────────────────────────────────────────────

test('the Report tab of a Bayesian model: the sampling\'s run, or the prior predictive check\'s, a trial of its own', async () => {
  const R = await import('~/composables/statReport.ts')
  const ok = { run_id: 5, status: 'SUCCESS', trial: true, stale: false, finished_at: '2026-11-24T10:00:00Z' }
  const prior = { run_id: 9, status: 'SUCCESS', trial: true, stale: true, finished_at: '2026-11-24T11:00:00Z' }
  const entry = { last: ok, output: ok, prior_check: prior }
  assert.deepEqual(R.statModelReportOf(entry, 'posterior'), { runId: 5, stale: false, trial: true, failed: false, view: 'posterior', hasPrior: true, at: ok.finished_at })
  assert.deepEqual(R.statModelReportOf(entry, 'prior'), { runId: 9, stale: true, trial: true, failed: false, view: 'prior', hasPrior: true, at: prior.finished_at })
  // no prior run: the sampling's pages, nothing to switch to; a prior that failed shows no run (and never the sampling's)
  assert.equal(R.statModelReportOf({ last: ok, output: ok }, 'prior').view, 'posterior')
  assert.equal(R.statModelReportOf({ last: ok, output: ok }, 'prior').hasPrior, false)
  const failed = R.statModelReportOf({ last: ok, output: ok, prior_check: { run_id: 10, status: 'FAILURE' } }, 'prior')
  assert.deepEqual([failed.runId, failed.failed, failed.view], [null, true, 'prior'])
  // a prior predictive check alone (no sampling yet) is still a page
  assert.equal(R.statModelReportOf({ last: null, output: null, prior_check: prior }, 'prior').runId, 9)
  assert.equal(R.statModelReportOf({ last: null, output: null, prior_check: prior }, 'posterior').runId, null)
})

test('the prior predictive check is wired: the button in the panel, the trial\'s body, the switch, the run page, five languages', () => {
  const panel = read('components/bayes/BayesNodePanel.vue')
  assert.match(panel, /v-if="checks === 'both'" class="block prior"/)
  assert.match(panel, /emit\('check'\)/)
  assert.match(read('components/ui/NodePanel.vue'), /@check="emit\('script-check', node\.id\)"/)
  const editor = read('components/FlowEditor.vue')
  assert.match(editor, /@script-check="\(id: string\) => runScriptNode\(id, 'prior_predictive'\)"/)
  assert.match(editor, /scriptingEditor\.runNode\(flowId\.value, nodeId, check\)/)
  assert.match(read('composables/useScripting.ts'), /body: \{ check \}/)
  const view = read('components/ui/StatReportView.vue')
  assert.match(view, /emit\('view', 'prior'\)/)
  const detail = read('components/models/ModelRunDetail.vue')
  assert.match(detail, /bayes_model/)
  assert.match(detail, /compile/)
  const keys = { bayesNode: ['prior.title', 'prior.hint', 'prior.run', 'prior.saveAndRun', 'prior.running', 'prior.last', 'prior.stale'],
    modelReport: ['switch', 'posterior', 'prior', 'priorShort', 'priorTitle', 'when', 'priorFailed'],
    'models.runDetail': ['savedModel', 'compile', 'compiling', 'compiled', 'compileReady', 'compileFailed', 'priorCheck'] }
  for (const l of LANGS) {
    const src = read(`i18n/locales/${l}.gen.ts`)
    for (const [ns, list] of Object.entries(keys)) {
      for (const k of list) {
        const leaf = k.split('.').pop()
        let at = ns === 'models.runDetail' ? src.indexOf('    "runDetail": {') : src.indexOf(`  "${ns}": {`)
        assert.ok(at > 0, `${l}: ${ns}`)
        if (ns === 'bayesNode') at = src.indexOf('    "prior": {', at)
        const text = src.slice(at, at + 1500)
        const m = new RegExp(`"${leaf}": "((?:[^"\\\\]|\\\\.)*)"`).exec(text)
        assert.ok(m, `${l}: ${ns}.${k}`)
        // no brace, bar or at sign but the placeholders (vue-i18n would read them as syntax)
        assert.doesNotMatch(m[1].replace(/\{(when|status|s)\}/g, ''), /[{}|@]/, `${l}: ${ns}.${k}`)
      }
    }
  }
})
