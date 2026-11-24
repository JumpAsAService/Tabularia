// The assistant of the Bayesian model editor, without a browser (docs/specs/bayes-assistants.md, lots A3 and A4;
// composables/bayesAssist.ts, components/bayes/BayesAssistant.vue): the request's body, a proposal placed on the
// graph by node id and by the Go validator's slot names, applied through the editor's own edit functions as ONE
// step of its history, never adding or deleting a node and never overwriting what the person changed since;
// what the check adds to the graph decides whether «Apply» is offered; the run's facts and what of them is
// sent; the picker's entries; the wiring of the page; and the words in five languages, with no verdict.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const A = await import('~/composables/bayesAssist.ts')
const { bayesOpenTemplate } = await import('~/composables/bayesTemplates.ts')
const { bayesGraphDecode, bayesGraphModel, bayesPriorPlan } = await import('~/composables/bayesGraph.ts')
const { bayesNodeSlots } = await import('~/composables/bayesGraphEdits.ts')
const { useBayesEditor } = await import('~/composables/useBayesEditor.ts')
const { bayesPriorPreview } = await import('~/composables/bayesPriorCore.ts')

const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const read = (p) => readFileSync(`${FRONTEND}${p}`, 'utf8')
const clone = (v) => JSON.parse(JSON.stringify(v))
const node = (g, id) => g.nodes.find(n => n.id === id)
const change = (node, field, value, extra = {}) => ({ node, field, slot: '', before: null, value, why: 'because', ...extra })
const proposal = (...changes) => ({ summary: 'a summary', changes })
const unitsOf = (g, ...changes) => A.bayesProposalUnits(g, proposal(...changes))
const modelOf = (g) => { const d = bayesGraphDecode(JSON.stringify(g)); assert.ok(d.graph); return bayesGraphModel(d.graph) }

// ── the request ──────────────────────────────────────────────────────────────

test('the body: the graph without its drawing, the findings with their words, the selection, the preview, the run, the version', async () => {
  const g = bayesOpenTemplate('hier_slopes')
  const plan = bayesPriorPlan(modelOf(g))
  const preview = await A.bayesAssistPreview(plan, 'taua', async (id) => (bayesPriorPreview(plan, id, 1)).quantiles ?? null)
  assert.deepEqual(Object.keys(preview), ['taua'])
  assert.deepEqual(Object.keys(preview.taua), ['q5', 'q50', 'q95'])
  const body = A.bayesAssistBody({
    message: 'hi', model: 'm1', chatId: 7, doc: g,
    findings: [{ code: 'graph.cycle', severity: 'error', node: 'mua', slot: 'prior.mu' }],
    say: f => `say ${f.code}`, selected: 'taua', preview, version: 3, saved: false, locale: 'it',
    run: { version: 2, sampler: { divergences: 4, bad: 'x' }, nodes: [{ node: 'mua', rhat_max: 1.002, summary: [{ mean: 5 }], ess_bulk_min: 800 }] },
  })
  assert.deepEqual(Object.keys(body).sort(), ['chat_id', 'findings', 'graph', 'locale', 'message', 'model', 'preview', 'run', 'saved', 'selected', 'version'])
  assert.equal(body.graph.layout, undefined)
  assert.equal(body.graph.nodes.length, g.nodes.length)
  assert.deepEqual(body.findings, [{ code: 'graph.cycle', severity: 'error', node: 'mua', slot: 'prior.mu', message: 'say graph.cycle' }])
  assert.equal(body.selected, 'taua')
  assert.equal(body.version, 3)
  assert.equal(body.saved, false)
  // of a run only the sampler's numbers and each node's extremes: no summary, no data, no text
  assert.deepEqual(body.run, { version: 2, sampler: { divergences: 4 }, nodes: [{ node: 'mua', rhat_max: 1.002, ess_bulk_min: 800 }] })
  assert.equal(JSON.stringify(body).includes('"summary":['), false)
})

test('no run, no selection: null and an empty preview; a graph over the limit is not sent', () => {
  const g = bayesOpenTemplate('glm_linear')
  const b = A.bayesAssistBody({ message: 'x', model: 'm', chatId: null, doc: g, findings: [], selected: null, version: null, saved: true, locale: 'en' })
  assert.equal(b.run, null)
  assert.deepEqual(b.preview, {})
  assert.equal(A.bayesAssistFits(g), true)
  const big = clone(g)
  big.nodes[0].note = 'x'.repeat(300000)
  assert.equal(A.bayesAssistFits(big), false)
  // measured in UTF-8 bytes, as the gateway does
  const wide = clone(g)
  wide.nodes[0].note = 'é'.repeat(140000)
  assert.equal(A.bayesAssistFits(wide), false)
})

test('the preview holds only the selected node\'s own numbers, never more than asked', async () => {
  const g = bayesOpenTemplate('hier_slopes')
  const plan = bayesPriorPlan(modelOf(g))
  const all = await A.bayesAssistPreview(plan, 'eff', () => ({ q5: 1, q50: 2, q95: 3 }))
  assert.ok(Object.keys(all).every(k => k === 'eff' || k.startsWith('eff.')))
  assert.ok(Object.keys(all).length >= 2)
  assert.equal(Object.keys(await A.bayesAssistPreview(plan, 'eff', () => ({ q5: 1, q50: 2, q95: 3 }), 1)).length, 1)
  assert.deepEqual(await A.bayesAssistPreview(plan, null, () => ({ q5: 1, q50: 2, q95: 3 })), {})
  assert.deepEqual(await A.bayesAssistPreview(plan, 'mua', () => ({ q5: NaN, q50: 2, q95: 3 })), {})
  assert.deepEqual(await A.bayesAssistPreview(plan, 'mua', () => { throw new Error('x') }), {})
})

// ── placing and applying a proposal ──────────────────────────────────────────

test('a prior\'s new distribution and its arguments are one unit, applied as typing them would', () => {
  const g = bayesOpenTemplate('hier_slopes')
  const before = { mu: '0', sigma: '2.5' }
  const us = unitsOf(g,
    change('mua', 'prior_distribution', 'student_t', { before: 'normal' }),
    change('mua', 'prior_args', { nu: '3', mu: '0', sigma: '2' }, { before }))
  assert.equal(us.length, 1)
  assert.equal(us[0].kind, 'prior')
  assert.equal(us[0].place, 'ok')
  assert.equal(us[0].before, 'normal(0, 2.5)')
  assert.equal(us[0].after, 'student_t(3, 0, 2)')
  const next = clone(g)
  assert.equal(A.bayesApplyUnits(next, us), 1)
  assert.equal(node(next, 'mua').prior.dist, 'student_t')
  assert.deepEqual(Object.fromEntries(Object.entries(node(next, 'mua').prior.args).map(([k, v]) => [k, v.expr])), { nu: '3', mu: '0', sigma: '2' })
  assert.equal(node(g, 'mua').prior.dist, 'normal')
  // the new graph is one the mirror accepts
  assert.deepEqual(modelOf(next).findings.filter(f => f.severity === 'error'), [])
})

test('arguments alone, a state\'s initial prior, and arguments that are not the distribution\'s are not placed', () => {
  const g = bayesOpenTemplate('ts_local_level')
  const ok = unitsOf(g, change('level', 'prior_args', { mu: '0', sigma: '3' }, { before: { mu: '0', sigma: '2.5' } }))
  assert.equal(ok[0].place, 'ok')
  const next = clone(g)
  A.bayesApplyUnits(next, ok)
  assert.equal(node(next, 'level').init.args.sigma.expr, '3')
  const wrong = unitsOf(g, change('level', 'prior_args', { mu: '0' }, { before: { mu: '0', sigma: '2.5' } }))
  assert.equal(wrong[0].place, 'missing')
  // a distribution change with no arguments of its own is never placed
  const alone = unitsOf(g, change('level', 'prior_distribution', 'student_t', { before: 'normal' }))
  assert.equal(alone[0].place, 'missing')
  // a likelihood or a data node has no prior to change
  assert.equal(unitsOf(g, change('lik', 'prior_args', { mu: '0', sigma: '1' }))[0].place, 'missing')
  assert.equal(unitsOf(g, change('y', 'prior_args', { mu: '0', sigma: '1' }))[0].place, 'missing')
})

test('an expression is placed by its slot, in the Go validator\'s keys, on a node, a component and the correlation', () => {
  const g = bayesOpenTemplate('hier_slopes')
  const unitFor = (nodeId, slot, value, before) => unitsOf(g, change(nodeId, 'expression', value, { slot, before }))[0]
  // a prior's argument
  const u = unitFor('taua', 'prior.sigma', '0.5', '1')
  assert.equal(u.place, 'ok')
  assert.equal(u.slot.path, 'prior.sigma')
  // a component of correlated effects is a node of its own: `<node>.<component>`, slot mean or scale
  assert.equal(unitFor('eff.ca', 'scale', 'tau_a * 2', 'tau_a').place, 'ok')
  assert.equal(unitFor('eff.cb', 'mean', 'mu_b', 'mu_b').place, 'same')
  // the correlation
  const rho = unitFor('eff.correlation', 'eta', '4', '2')
  assert.equal(rho.place, 'ok')
  const next = clone(g)
  A.bayesApplyUnits(next, [u, unitFor('eff.ca', 'scale', 'tau_a * 2', 'tau_a'), rho])
  assert.equal(node(next, 'taua').prior.args.sigma.expr, '0.5')
  assert.equal(node(next, 'eff').components[0].scale.expr, 'tau_a * 2')
  assert.equal(node(next, 'eff').correlation.eta.expr, '4')
  // a slot the node has not, a component that is not there, and the older spellings
  assert.equal(unitFor('taua', 'prior.nope', '1', '1').place, 'missing')
  assert.equal(unitFor('eff.cz', 'mean', '1', '1').place, 'missing')
  assert.equal(unitFor('eff.correlation', 'mean', '1', '1').place, 'missing')
  const lp = bayesOpenTemplate('glm_linear')
  assert.equal(unitsOf(lp, change('eta', 'expression', '0', { slot: 'terms[0].coef', before: 'b_x' }))[0].place, 'ok')
  assert.equal(unitsOf(lp, change('eta', 'expression', '0', { slot: 'coef:0', before: 'b_x' }))[0].place, 'ok')
  assert.equal(unitsOf(lp, change('eta', 'expression', 'alpha', { slot: 'intercept', before: 'alpha' }))[0].place, 'same')
  // a data node's slot (observed, a term's data) is not an expression
  assert.equal(unitsOf(lp, change('lik', 'expression', 'x', { slot: 'observed', before: 'y' }))[0].place, 'missing')
  assert.equal(unitsOf(lp, change('eta', 'expression', 'x', { slot: 'terms[0].data', before: 'x' }))[0].place, 'missing')
})

test('a state\'s scale and a transformed node\'s value', () => {
  const g = bayesOpenTemplate('ts_local_level')
  const u = unitsOf(g, change('level', 'expression', 'sigma_level / 2', { slot: 'scale', before: 'sigma_level' }))[0]
  assert.equal(u.place, 'ok')
  const next = clone(g)
  A.bayesApplyUnits(next, [u])
  assert.equal(node(next, 'level').scale.expr, 'sigma_level / 2')
})

test('bounds: a bound set, a bound removed with null, the other kept; a node that is no parameter has none', () => {
  const g = bayesOpenTemplate('hier_slopes')
  assert.deepEqual(node(g, 'taua').bounds, { lower: 0 })
  const set = unitsOf(g, change('taua', 'bounds', { upper: 5 }, { before: { lower: 0 } }))[0]
  assert.equal(set.place, 'ok')
  assert.equal(set.before, '≥ 0')
  assert.equal(set.after, '0 … 5')
  const next = clone(g)
  A.bayesApplyUnits(next, [set])
  assert.deepEqual(node(next, 'taua').bounds, { lower: 0, upper: 5 })
  const drop = unitsOf(next, change('taua', 'bounds', { lower: null, upper: null }, { before: { lower: 0, upper: 5 } }))[0]
  A.bayesApplyUnits(next, [drop])
  assert.equal(node(next, 'taua').bounds, undefined)
  assert.equal(unitsOf(g, change('lik', 'bounds', { lower: 0 }))[0].place, 'missing')
  assert.equal(unitsOf(g, change('taua', 'bounds', 'x'))[0].place, 'missing')
})

test('a box\'s form and a state\'s form', () => {
  const g = bayesOpenTemplate('hier_slopes')
  const u = unitsOf(g, change('grp', 'plate_form', 'centred', { before: 'non_centred' }))[0]
  assert.equal(u.place, 'ok')
  const next = clone(g)
  A.bayesApplyUnits(next, [u])
  assert.equal(next.plates.find(p => p.id === 'grp').form, 'centred')
  assert.equal(unitsOf(next, change('grp', 'plate_form', 'centred', { before: 'non_centred' }))[0].place, 'same')
  assert.equal(unitsOf(g, change('rows', 'plate_form', 'centred'))[0].place, 'missing')
  const ts = bayesOpenTemplate('ts_local_level')
  const s = unitsOf(ts, change('level', 'plate_form', 'centred', { before: 'non_centred' }))[0]
  assert.equal(s.place, 'ok')
  A.bayesApplyUnits(ts, [s])
  assert.equal(node(ts, 'level').form, 'centred')
  assert.equal(unitsOf(g, change('grp', 'plate_form', 'sideways'))[0].place, 'missing')
})

test('a change is placed against the graph as it is now: what the person changed since is never overwritten', () => {
  const g = bayesOpenTemplate('hier_slopes')
  const ch = change('taua', 'expression', '0.5', { slot: 'prior.sigma', before: '1' })
  assert.equal(unitsOf(g, ch)[0].place, 'ok')
  // the person typed another value after the question
  const edited = clone(g)
  node(edited, 'taua').prior.args.sigma = { expr: '3' }
  const u = unitsOf(edited, ch)[0]
  assert.equal(u.place, 'moved')
  const before = JSON.stringify(edited)
  assert.equal(A.bayesApplyUnits(edited, [u]), 0)
  assert.equal(JSON.stringify(edited), before)
  // the node was deleted meanwhile
  const gone = clone(g)
  gone.nodes = gone.nodes.filter(n => n.id !== 'taua')
  assert.equal(unitsOf(gone, ch)[0].place, 'missing')
  // a prior whose distribution changed since
  const swapped = clone(g)
  A.bayesApplyUnits(swapped, unitsOf(swapped, change('mua', 'prior_distribution', 'student_t', { before: 'normal' }), change('mua', 'prior_args', { nu: '3', mu: '0', sigma: '1' }, { before: { mu: '0', sigma: '2.5' } })))
  assert.equal(unitsOf(swapped, change('mua', 'prior_args', { mu: '0', sigma: '1' }, { before: { mu: '0', sigma: '2.5' } }))[0].place, 'missing')
})

test('a reference in a slot reads as the id it names', () => {
  const g = bayesOpenTemplate('hier_slopes')
  node(g, 'taua').prior.args.sigma = { ref: 'taub' }
  assert.equal(unitsOf(g, change('taua', 'expression', '1', { slot: 'prior.sigma', before: { ref: 'taub' } }))[0].place, 'ok')
  assert.equal(unitsOf(g, change('taua', 'expression', '1', { slot: 'prior.sigma', before: 'taub' }))[0].place, 'ok')
})

test('a proposal never adds or deletes a node or a box, whatever it says', () => {
  const g = bayesOpenTemplate('hier_slopes')
  const ids = (d) => [...d.nodes.map(n => n.id), ...d.plates.map(p => p.id)].sort()
  const next = clone(g)
  const hostile = [
    change('brand_new', 'prior_args', { mu: '0', sigma: '1' }),
    change('mua', 'expression', '0', { slot: 'prior.mu', before: '0' }),
    change('grp', 'plate_form', 'centred', { before: 'non_centred' }),
    change('delete_everything', 'bounds', { lower: 0 }),
    { node: 'mua', field: 'delete_node', slot: '', before: null, value: null, why: '' },
  ]
  const p = A.bayesReadProposal({ summary: 's', changes: hostile })
  assert.equal(p.changes.length, 4) // the unknown field is dropped on reading
  A.bayesApplyUnits(next, A.bayesProposalUnits(next, p))
  assert.deepEqual(ids(next), ids(g))
  assert.equal(next.nodes.length, g.nodes.length)
  assert.equal(next.layout, g.layout === undefined ? undefined : next.layout)
})

test('the proposal is read from `done` without trusting its shape', () => {
  assert.equal(A.bayesReadProposal(null), null)
  assert.equal(A.bayesReadProposal({ changes: [] }), null)
  assert.equal(A.bayesReadProposal({ changes: 'x' }), null)
  assert.equal(A.bayesReadProposal({ changes: [{ node: 3, field: 'expression' }] }), null)
  const p = A.bayesReadProposal({ summary: 7, changes: [{ node: 'a', field: 'bounds', value: {}, why: 9 }] })
  assert.deepEqual(p, { summary: '', changes: [{ node: 'a', field: 'bounds', slot: '', before: undefined, value: {}, why: '' }] })
})

// ── apply is one step of the editor's history ────────────────────────────────

test('applying a proposal is one step of the history: undo brings the graph back as it was', () => {
  const editor = useBayesEditor({ settleMs: 5, schedule: () => 0, cancel: () => {} })
  editor.load(bayesOpenTemplate('hier_slopes'))
  const was = editor.text.value
  const us = A.bayesProposalUnits(editor.doc.value, proposal(
    change('mua', 'prior_distribution', 'student_t', { before: 'normal' }),
    change('mua', 'prior_args', { nu: '3', mu: '0', sigma: '2' }, { before: { mu: '0', sigma: '2.5' } }),
    change('taua', 'expression', '0.5', { slot: 'prior.sigma', before: '1' }),
    change('grp', 'plate_form', 'centred', { before: 'non_centred' })))
  assert.equal(us.length, 3)
  assert.equal(us.every(u => u.place === 'ok'), true)
  assert.equal(editor.canUndo.value, false)
  editor.edit(d => { A.bayesApplyUnits(d, us) }, true)
  assert.notEqual(editor.text.value, was)
  assert.equal(editor.dirty.value, true)
  assert.equal(editor.canUndo.value, true)
  // one undo takes the whole proposal back
  assert.equal(editor.undo(), true)
  assert.equal(editor.text.value, was)
  assert.equal(editor.canUndo.value, false)
  assert.equal(editor.redo(), true)
  assert.notEqual(editor.text.value, was)
  // after the edit the same units read as already in the graph
  assert.equal(A.bayesProposalUnits(editor.doc.value, proposal(change('taua', 'expression', '0.5', { slot: 'prior.sigma', before: '1' })))[0].place, 'same')
  editor.dispose()
})

test('a version looked at takes no edit, so nothing is applied', () => {
  const editor = useBayesEditor({ settleMs: 5, schedule: () => 0, cancel: () => {} })
  editor.load(bayesOpenTemplate('hier_slopes'))
  editor.readonly.value = true
  const was = editor.text.value
  const us = A.bayesProposalUnits(editor.doc.value, proposal(change('taua', 'expression', '0.5', { slot: 'prior.sigma', before: '1' })))
  editor.edit(d => { A.bayesApplyUnits(d, us) }, true)
  assert.equal(editor.text.value, was)
  editor.dispose()
})

// ── the check before «Apply» ─────────────────────────────────────────────────

test('what the check adds decides «Apply»: a new error withholds it, a warning or an old error does not', () => {
  const f = (code, severity, node = 'mua', slot = '') => ({ code, severity, node, slot })
  const now = [f('graph.unused_data', 'warning', 'x'), f('graph.cycle', 'error', 'a')]
  // the gateway refuses an expression it cannot read: that is a new error
  const refused = A.bayesVerdictOf(now, [...now, f('expr.syntax', 'error', 'taua', 'prior.sigma')])
  assert.equal(refused.applicable, false)
  assert.deepEqual(refused.added.map(x => x.code), ['expr.syntax'])
  // a warning is shown and blocks nothing
  const warned = A.bayesVerdictOf(now, [...now, f('graph.constant', 'warning', 'taua')])
  assert.equal(warned.applicable, true)
  assert.equal(warned.added.length, 1)
  // an error the graph already had is not the proposal's
  const same = A.bayesVerdictOf(now, now)
  assert.equal(same.applicable, true)
  assert.deepEqual(same.added, [])
  // the same code on another node or slot is another finding
  assert.equal(A.bayesVerdictOf(now, [...now, f('graph.cycle', 'error', 'b')]).applicable, false)
})

test('the proposed graph is a copy: the graph on the canvas is not touched while the gateway reads it', () => {
  const g = bayesOpenTemplate('hier_slopes')
  const was = JSON.stringify(g)
  const us = unitsOf(g, change('taua', 'expression', '0.5', { slot: 'prior.sigma', before: '1' }))
  const next = A.bayesProposedGraph(g, us)
  assert.equal(JSON.stringify(g), was)
  assert.equal(node(next, 'taua').prior.args.sigma.expr, '0.5')
  // a unit that cannot be placed is left out of the copy
  const moved = unitsOf(g, change('taua', 'expression', '0.5', { slot: 'prior.sigma', before: '9' }))
  assert.equal(JSON.stringify(A.bayesProposedGraph(g, moved)), was)
})

// ── reading a run ────────────────────────────────────────────────────────────

test('the picker: a node never run is listed and cannot be chosen; a development run says so', () => {
  const list = [
    { flow_id: 1, flow_name: 'Sales', node_id: 'n1', node_label: 'Model', pinned_version: 2, run_id: 11, version: 2, trial: false, finished_at: null },
    { flow_id: 1, flow_name: 'Sales', node_id: 'n2', node_label: 'Trial', pinned_version: 2, run_id: 12, version: 1, trial: true, finished_at: null },
    { flow_id: 2, flow_name: 'Costs', node_id: 'n1', node_label: 'Never', pinned_version: 3, run_id: null, version: null, trial: false, finished_at: null },
  ]
  const c = A.bayesRunChoices(list)
  assert.deepEqual(c.map(x => x.disabled), [false, false, true])
  assert.deepEqual(c.map(x => x.trial), [false, true, false])
  assert.equal(new Set(c.map(x => x.key)).size, 3)
  assert.equal(A.bayesRunOtherVersion(1, 2), true)
  assert.equal(A.bayesRunOtherVersion(2, 2), false)
  assert.equal(A.bayesRunOtherVersion(2, null), false)
  assert.equal(A.bayesRunOtherVersion(undefined, 2), false)
})

// ── the page, the client and the component ───────────────────────────────────

test('the page offers the tab only where the assistant is configured and the person may use it, and applies as one edit', () => {
  const page = read('pages/bayes-model.vue')
  assert.match(page, /aiStatus\.value\?\.enabled && !!aiStatus\.value\.models\.length && !user\.value\?\.is_observer && mode\.value !== 'preview'/)
  assert.match(page, /<button v-if="aiAvailable" id="bt-assistant"/)
  assert.match(page, /function applyAssistant \(fn: \(d: BayesDoc\) => void\) \{ editor\.edit\(\(d\) => \{ fn\(d\) \}, true\) \}/)
  assert.match(page, /@apply="applyAssistant"/)
  assert.match(page, /:read-only="readonly"/)
  // a node chosen on the canvas does not take the person away from the assistant
  assert.match(page, /tab\.value !== 'code' && tab\.value !== 'assistant'/)
  // the assistant is mounted when first opened and kept while the tabs change
  assert.match(page, /v-if="aiAvailable && aiStatus && assistantOpened" v-show="tab === 'assistant'"/)
})

test('the client: the route, the two run routes, the admin panel\'s agent', () => {
  const ai = read('composables/useAi.ts')
  assert.match(ai, /stream\('\/ai\/bayes-assist', body, handlers, signal\)/)
  assert.match(ai, /export type AiAgent = [^\n]*'bayes'/)
  const store = read('composables/useBayesModels.ts')
  assert.match(store, /`\/bayes-models\/\$\{id\}\/runs`/)
  assert.match(store, /`\/bayes-models\/\$\{id\}\/runs\/\$\{run\}\/facts`/)
})

test('the component writes every label, answer and reason as text, never as markup, and checks before it offers «Apply»', () => {
  const vue = read('components/bayes/BayesAssistant.vue')
  assert.doesNotMatch(vue, /v-html|innerHTML|insertAdjacentHTML/)
  // the check is asked of the gateway on the proposed graph; «Apply» is enabled only by a finished, applicable check
  assert.match(vue, /props\.client\.check\(graph\)/)
  assert.match(vue, /c\?\.state === 'done' && c\.applicable/)
  assert.match(vue, /:disabled="!canApply\(turn, '\*'\)"/)
  assert.match(vue, /:disabled="!canApply\(turn, u\.key\)"/)
  // the findings go back in the next message
  assert.match(vue, /sendBack/)
  // no row of data and no posterior number: the body is built by the pure module, the run is the facts only
  assert.match(vue, /bayesAssistBody\(/)
  assert.match(vue, /run: facts\.value/)
  // a run is read only with a model id, and a run never run cannot be chosen
  assert.match(vue, /:disabled="r\.disabled"/)
  // no key of its own is needed: Enter sends, as the script assistant's does
  assert.doesNotMatch(vue, /F\d{1,2}\b|Home|End'|ContextMenu/)
})

// ── the words ────────────────────────────────────────────────────────────────

const LANGS = {}
for (const l of ['en', 'it', 'de', 'es', 'fr']) LANGS[l] = (await import(`../i18n/locales/${l}.gen.ts`)).default
const leaf = (o, p = '') => Object.entries(o).flatMap(([k, v]) => (typeof v === 'object' ? leaf(v, `${p}${k}.`) : [[`${p}${k}`, v]]))
const placeholders = (s) => [...new Set([...s.matchAll(/\{(\w+)\}/g)].map(m => m[1]))].sort()

// the editor's list of judging words (tests/bayes-editor-words.test.mjs) and the verdicts of the spec's rule 1
const VERDICTS = /\b(wide|narrow|vague|informative|reasonable|weak|strong|significant|plausible|good|bad|large|small|better|worse|converged|reliable|problems?|passes|fails)\b|\b(ampio|ampia|stretto|vago|informativo|ragionevole|debole|forte|significativo|plausibile|peggio|meglio|buono|buona|cattivo|convergito|affidabile|problemi?|supera|fallisce)\b|\b(breit|schmal|vage|informativ|vernünftig|schwach|stark|signifikant|plausibel|schlechter|besser|gut|schlecht|konvergiert|zuverlässig|problem(e)?|besteht|scheitert)\b|\b(amplio|amplia|estrecho|vago|informativo|razonable|débil|fuerte|significativo|plausible|peor|mejor|bueno|malo|convergido|fiable|problemas?|supera|falla)\b|\b(large|étroit|vague|informatif|raisonnable|faible|fort|significatif|plausible|meilleur|mauvais|bon|convergé|fiable|problèmes?|réussit|échoue)\b|less well|moins bien|meno bene/i

test('every key of the assistant in the five languages, the same placeholders, no syntax of vue-i18n in the words', () => {
  const en = new Map(leaf(LANGS.en.bayesAssistant, 'bayesAssistant.'))
  assert.ok(en.size > 50, String(en.size))
  for (const [lang, tree] of Object.entries(LANGS)) {
    const words = new Map(leaf(tree.bayesAssistant, 'bayesAssistant.'))
    assert.deepEqual([...words.keys()].sort(), [...en.keys()].sort(), lang)
    for (const [key, text] of words) {
      assert.equal(typeof text, 'string', `${lang} ${key}`)
      assert.ok(text.length > 0, `${lang} ${key}`)
      assert.deepEqual(placeholders(text), placeholders(en.get(key)), `${lang} ${key}`)
      assert.doesNotMatch(text, /[|@]/, `${lang} ${key}: | or @`)
      assert.ok(!/[{}]/.test(text.replace(/\{[a-zA-Z_]+\}/g, '')), `${lang} ${key}: stray brace`)
    }
    // the admin panel's agent
    assert.equal(typeof tree.aiPrompts.agents.bayes.name, 'string', lang)
    assert.equal(typeof tree.aiPrompts.agents.bayes.purpose, 'string', lang)
  }
})

test('no word of the assistant gives a verdict, in any language', () => {
  for (const [lang, tree] of Object.entries(LANGS)) {
    const words = [...leaf(tree.bayesAssistant, 'bayesAssistant.'), ['aiPrompts.agents.bayes.name', tree.aiPrompts.agents.bayes.name], ['aiPrompts.agents.bayes.purpose', tree.aiPrompts.agents.bayes.purpose]]
    for (const [key, text] of words) assert.doesNotMatch(text, VERDICTS, `${lang} ${key}: ${text}`)
  }
})

test('the words the component uses exist, the proposal\'s places included', () => {
  const vue = read('components/bayes/BayesAssistant.vue')
  const used = new Set([...vue.matchAll(/t\('(bayesAssistant\.[\w.]+)'/g)].map(m => m[1]))
  const dynamic = ['bayesAssistant.place.missing', 'bayesAssistant.place.moved', 'bayesAssistant.place.same', 'bayesAssistant.kind.prior', 'bayesAssistant.kind.bounds', 'bayesAssistant.kind.form', 'bayesAssistant.form.centred', 'bayesAssistant.form.non_centred']
  for (const k of [...used, ...dynamic]) {
    for (const [lang, tree] of Object.entries(LANGS)) {
      const v = k.split('.').reduce((o, p) => (o == null ? undefined : o[p]), tree)
      assert.equal(typeof v, 'string', `${lang} ${k}`)
    }
  }
  assert.ok(used.size > 25, String(used.size))
})
