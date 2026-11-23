// The inspector of the Bayesian editor without a browser (docs/specs/bayes-graph-editor.md, «The
// inspector», lot G2): the edits of each kind's own fields, what the inspector reads of a slot from
// the mirror, the names offered at the caret, the order of the Problems tab, and every finding of the
// mirror's corpus on a node or a box the canvas draws (or on the model itself).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const E = await import('~/composables/bayesGraphEdits.ts')
const I = await import('~/composables/bayesInspector.ts')
const { bayesFindingText } = await import('~/composables/bayesEditorText.ts')
const { bayesOpenTemplate, bayesTemplates } = await import('~/composables/bayesTemplates.ts')
const { bayesGraphDecode, bayesGraphModel } = await import('~/composables/bayesGraph.ts')

const open = (id) => bayesOpenTemplate(id, {})
const mirror = (doc) => {
  const d = bayesGraphDecode(JSON.stringify(doc))
  const model = d.graph ? bayesGraphModel(d.graph) : null
  return { model, findings: [...d.findings, ...(model?.findings ?? [])] }
}
const node = (doc, id) => doc.nodes.find(n => n.id === id)

test('a distribution changed keeps the arguments of the same name, writes the plainest for the new ones', () => {
  const doc = open('glm_linear')
  assert.equal(E.bayesSetDist(doc, 'alpha', 'prior', 'student_t'), true)
  assert.deepEqual(node(doc, 'alpha').prior, { dist: 'student_t', args: { nu: { expr: '1' }, mu: { expr: '0' }, sigma: { expr: '2.5' } } })
  assert.equal(E.bayesSetDist(doc, 'alpha', 'prior', 'student_t'), false)
  E.bayesSetDist(doc, 'alpha', 'prior', 'uniform')
  assert.deepEqual(node(doc, 'alpha').prior.args, { alpha: { expr: '0' }, beta: { expr: '1' } })
  // a likelihood's new arguments are left for the person to connect
  E.bayesSetDist(doc, 'lik', 'likelihood', 'neg_binomial_2_log')
  assert.deepEqual(node(doc, 'lik').args, { eta: {}, phi: {} })
  assert.equal(node(doc, 'lik').dist, 'neg_binomial_2_log')
  const st = open('ts_local_level')
  E.bayesSetDist(st, 'level', 'init', 'cauchy')
  assert.deepEqual(node(st, 'level').init, { dist: 'cauchy', args: { mu: { expr: '0' }, sigma: { expr: '2.5' } } })
  assert.equal(E.bayesSetDist(st, 'level', 'init', 'no_such'), false)
})

test('a data node\'s type brings its own fields and drops the old type\'s', () => {
  const doc = open('glm_linear')
  E.bayesSetDataType(doc, 'x', 'int')
  assert.equal(node(doc, 'x').support, 'any')
  assert.equal('standardize' in node(doc, 'x'), false)
  E.bayesSetSupport(doc, 'x', 'binary')
  assert.equal(node(doc, 'x').support, 'binary')
  E.bayesSetDataType(doc, 'x', 'group_index')
  assert.equal(node(doc, 'x').target, '')
  assert.equal('support' in node(doc, 'x'), false)
  E.bayesSetDataType(doc, 'x', 'real')
  assert.equal(node(doc, 'x').standardize, true)
  // the outcome a likelihood observes is never standardized
  E.bayesSetDataType(doc, 'y', 'int')
  E.bayesSetDataType(doc, 'y', 'real')
  assert.equal('standardize' in node(doc, 'y'), false)
  E.bayesSetStandardize(doc, 'x', false)
  assert.equal('standardize' in node(doc, 'x'), false)
  assert.equal(mirror(doc).findings.filter(f => f.severity === 'error').length, 0)
})

test('terms, components, a state\'s form and a note, as the inspector writes them', () => {
  const doc = open('glm_linear')
  assert.equal(E.bayesAddTerm(doc, 'eta'), 1)
  assert.deepEqual(node(doc, 'eta').terms[1], { data: '', coef: {} })
  E.bayesWriteSlot(doc, 'eta', 'terms[1].data', 'y')
  E.bayesRemoveTerm(doc, 'eta', 1)
  assert.equal(node(doc, 'eta').terms.length, 1)
  E.bayesSetNote(doc, 'eta', 'From the brief')
  assert.equal(node(doc, 'eta').note, 'From the brief')
  E.bayesSetNote(doc, 'eta', '')
  assert.equal('note' in node(doc, 'eta'), false)

  const h = open('hier_slopes')
  const cid = E.bayesAddComponent(h, 'eff', 'Effect 3', 10)
  assert.equal(cid, 'c1')
  const c = node(h, 'eff').components.find(x => x.id === cid)
  assert.deepEqual({ name: c.name, mean: c.mean, scale: c.scale }, { name: 'effect_3', mean: { expr: '0' }, scale: { expr: '1' } })
  // the name follows the label while it is the one proposed from it
  E.bayesSetEffectsText(h, 'eff', cid, 'label', 'Slope of age')
  assert.equal(c.name, 'slope_of_age')
  E.bayesSetEffectsText(h, 'eff', cid, 'name', 'b_age')
  E.bayesSetEffectsText(h, 'eff', cid, 'label', 'Age')
  assert.equal(c.name, 'b_age')
  E.bayesSetEffectsText(h, 'eff', 'correlation', 'name', 'omega')
  assert.equal(node(h, 'eff').correlation.name, 'omega')
  assert.equal(E.bayesAddComponent(h, 'eff', 'x', 3), '')
  E.bayesRemoveComponent(h, 'eff', cid)
  assert.equal(node(h, 'eff').components.length, 2)

  const s = open('ts_local_level')
  E.bayesSetStateForm(s, 'level', false)
  assert.equal(node(s, 'level').form, 'centred')
})

test('a slot\'s view: the first error and its span, the type, the hyperprior it reads', () => {
  const doc = open('hier_intercepts')
  const g = doc.nodes.find(n => n.kind === 'parameter' && n.plate)
  let { model, findings } = mirror(doc)
  const sigma = E.bayesNodeSlots(doc, g.id).find(s => s.path === 'prior.sigma')
  const v = I.bayesSlotView(sigma, findings, model)
  assert.equal(v.error, null)
  assert.equal(v.typed.type.plate, '')
  assert.ok(v.hyper, 'the spread between groups is a hyperprior')
  E.bayesWriteSlot(doc, g.id, 'prior.mu', { expr: 'mu_a + nope' })
  ;({ model, findings } = mirror(doc))
  const mu = E.bayesNodeSlots(doc, g.id).find(s => s.path === 'prior.mu')
  const bad = I.bayesSlotView(mu, findings, model)
  assert.equal(bad.error.code, 'expr.unknown_name')
  assert.deepEqual([bad.pos, bad.len], [7, 4])
  assert.equal(bad.hyper, '')
  // an error with no span underlines the whole text
  E.bayesWriteSlot(doc, g.id, 'prior.sigma', { expr: '-1' })
  ;({ model, findings } = mirror(doc))
  const neg = I.bayesSlotView(E.bayesNodeSlots(doc, g.id).find(s => s.path === 'prior.sigma'), findings, model)
  assert.equal(neg.error.code, 'graph.slot_interval')
  assert.deepEqual([neg.pos, neg.len], [0, 2])
})

test('the words of a value\'s plate', () => {
  const doc = open('hier_intercepts')
  const group = doc.plates.find(p => p.kind === 'group').id
  assert.deepEqual(I.bayesPlateWords('', doc), { key: 'single' })
  assert.deepEqual(I.bayesPlateWords('rows', doc), { key: 'perRow' })
  assert.deepEqual(I.bayesPlateWords(group, doc), { key: 'perUnit', plate: group })
  assert.deepEqual(I.bayesPlateWords('levels:n4', doc), { key: 'perLevel', node: 'n4' })
  assert.equal(I.bayesIsPriorPath('prior.mu'), true)
  assert.equal(I.bayesIsPriorPath('init.sigma'), true)
  assert.equal(I.bayesIsPriorPath('scale'), true)
  assert.equal(I.bayesIsPriorPath('c1.mean'), true)
  assert.equal(I.bayesIsPriorPath('correlation.eta'), true)
  assert.equal(I.bayesIsPriorPath('args.mu'), false)
  assert.equal(I.bayesIsPriorPath('terms[0].coef'), false)
  assert.equal(I.bayesIsPriorPath('value'), false)
})

test('the names at the caret: what fits there, filtered, and written in place', () => {
  const doc = open('ts_local_level')
  const any = I.bayesNameChoices(doc, false).map(c => c.text)
  const prior = I.bayesNameChoices(doc, true).map(c => c.text)
  // data in a likelihood, never in a prior; a time node never; lag only where there is data
  assert.ok(any.includes('y') && !prior.includes('y'))
  assert.ok(!any.includes('t'))
  assert.ok(any.includes('lag') && !prior.includes('lag'))
  assert.ok(prior.includes('sigma_level') && prior.includes('exp'))
  const h = open('hier_slopes')
  const names = I.bayesNameChoices(h, true)
  assert.ok(names.some(c => c.text === 'a_g' && c.role === 'component' && c.node === 'eff.ca'))
  assert.ok(!names.some(c => c.text === 'rho_g'), 'a correlation is not a number')
  // the word at the caret, a number is not one
  assert.deepEqual(I.bayesWordAt('mu_a + ta', 9), { start: 7, end: 9, word: 'ta' })
  assert.deepEqual(I.bayesWordAt('mu_a + tau_a', 9), { start: 7, end: 12, word: 'ta' })
  assert.equal(I.bayesWordAt('2.5', 3).word, '')
  assert.equal(I.bayesWordAt('', 0).word, '')
  const list = I.bayesFilterNames(names, 'ta')
  assert.deepEqual(list.map(c => c.text).slice(0, 2), ['tau_a', 'tau_b'])
  assert.deepEqual(I.bayesFilterNames(names, 'tau_a').map(c => c.text), [], 'a name written whole offers nothing more')
  assert.ok(I.bayesFilterNames(names, 'g').every(c => c.text.includes('g')))
  assert.equal(I.bayesFilterNames(names, '', 3).length, 3)
  const tau = names.find(c => c.text === 'tau_a')
  assert.deepEqual(I.bayesInsertName('mu_a + ta * 2', 9, tau), { text: 'mu_a + tau_a * 2', caret: 12 })
  const exp = names.find(c => c.text === 'exp')
  assert.deepEqual(I.bayesInsertName('ex', 2, exp), { text: 'exp(', caret: 4 })
  assert.deepEqual(I.bayesInsertName('ex(1)', 2, exp), { text: 'exp(1)', caret: 4 })
})

test('the Problems tab lists errors first, the model\'s own first, then in the order of the document', () => {
  const doc = open('glm_linear')
  const fs = [
    { code: 'graph.unused_parameter', severity: 'warning', node: 'alpha', slot: '', args: {} },
    { code: 'graph.slot', severity: 'error', node: 'lik', slot: 'args.mu', args: { reason: 'empty' } },
    { code: 'graph.likelihood', severity: 'error', node: '', slot: '', args: { reason: 'none' } },
    { code: 'graph.slot', severity: 'error', node: 'alpha', slot: 'prior.mu', args: { reason: 'empty' } },
    { code: 'graph.plate_empty', severity: 'warning', node: 'rows', slot: '', args: {} },
  ]
  const out = I.bayesProblems(fs, doc).map(p => `${p.finding.severity}:${p.owner}`)
  assert.deepEqual(out, ['error:', 'error:alpha', 'error:lik', 'warning:rows', 'warning:alpha'])
})

test('the nodes of a message are named by their labels', () => {
  const labels = { y: 'Outcome', eta: 'Expected outcome', rows: 'Rows' }
  const t = bayesFindingText({ code: 'graph.prior_data', severity: 'error', node: 'a', slot: 'prior.mu', args: { data: 'y', through: 'eta' } }, id => labels[id])
  assert.equal(t.key, 'bayes.graph.prior_data.through')
  assert.deepEqual(t.params, { data: 'Outcome', through: 'Expected outcome' })
  const s = bayesFindingText({ code: 'graph.self_predictor', severity: 'error', node: 'lik', slot: 'args.mu', args: { observed: 'y' } }, id => labels[id])
  assert.equal(s.params.observed, 'Outcome')
  // an id nobody knows stays as it is
  const u = bayesFindingText({ code: 'graph.likelihood', severity: 'error', node: 'lik', slot: 'observed', args: { reason: 'unknown_node', observed: 'gone' } }, id => labels[id])
  assert.equal(u.params.observed, 'gone')
})

test('every finding of the mirror\'s corpus and of the templates lands on a node or a box the canvas draws, or on the model', () => {
  const corpus = JSON.parse(readFileSync(fileURLToPath(new URL('../../gateway-go/internal/bayesgraph/testdata/mirror/cases.json', import.meta.url)), 'utf8'))
  let checked = 0
  const largest = { size: 0, text: '' }
  for (const c of corpus.cases) {
    if (!c.decoded) continue
    const d = bayesGraphDecode(c.input)
    if (!d.graph) continue
    if (c.input.length > largest.size) { largest.size = c.input.length; largest.text = c.input }
    const ids = new Set([...d.graph.nodes.map(n => n.id), ...d.graph.plates.map(p => p.id)])
    const model = bayesGraphModel(d.graph)
    for (const f of [...d.findings, ...model.findings]) {
      const owner = f.node.includes('.') ? f.node.slice(0, f.node.indexOf('.')) : f.node
      // a graph.id finding names an id that is broken on purpose: the canvas cannot draw that node
      if (f.code === 'graph.id' || f.code === 'graph.rows') continue
      assert.ok(owner === '' || ids.has(owner), `${c.id}: ${f.code} on ${f.node}`)
      checked++
    }
  }
  for (const tpl of bayesTemplates()) assert.equal(mirror(open(tpl.id)).findings.filter(f => f.severity === 'error').length, 0, tpl.id)
  assert.ok(checked > 200, String(checked))
  // the mirror's time on the corpus's largest graph (recorded in the spec's «As built», not asserted)
  const t0 = performance.now()
  for (let i = 0; i < 20; i++) { const g = bayesGraphDecode(largest.text).graph; bayesGraphModel(g) }
  const ms = (performance.now() - t0) / 20
  assert.ok(Number.isFinite(ms))
})
