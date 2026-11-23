// The names the editor of Bayesian models proposes (composables/bayesNames.ts): from labels in the
// five languages, with accents, other alphabets and look-alike characters, a name that passes the
// graph's rules (the mirror finds no graph.name, graph.name_taken nor graph.name_function on it), and
// unique; the ids of new items unique and well formed.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const { bayesNameFromLabel, bayesUniqueName, bayesNameAllowed, bayesTakenNames, bayesNewId, BAYES_NAME_RULE, BAYES_NAME_PROPOSAL_CHARS } = await import('~/composables/bayesNames.ts')
const { bayesOpenTemplate } = await import('~/composables/bayesTemplates.ts')
const { bayesGraphDecode, bayesGraphModel } = await import('~/composables/bayesGraph.ts')

test('labels in five languages give names of the alphabet', () => {
  const cases = [
    ['Score of the school', 'score_of_the_school'],
    ['Livello della scuola', 'livello_della_scuola'],
    ['Niveau de l’école', 'niveau_de_l_ecole'],
    ['Größe der Filiale', 'grosse_der_filiale'],
    ['Año de la campaña', 'ano_de_la_campana'],
    ['μ della scuola', 'mu_della_scuola'],
    ['  --Spread--  ', 'spread'],
    ['2024 sales', 'v_2024_sales'],
    ['Œuvre', 'oeuvre'],
  ]
  for (const [label, name] of cases) {
    assert.equal(bayesNameFromLabel(label), name, label)
    assert.match(name, BAYES_NAME_RULE)
  }
  assert.equal(bayesNameFromLabel('∑∏≠'), '', 'nothing to fold')
  assert.equal(bayesNameFromLabel('Школа'), '', 'another alphabet folds to nothing')
})

test('a long label is cut at a word', () => {
  const n = bayesNameFromLabel('Spread of the levels between schools of a district')
  assert.ok(n.length <= BAYES_NAME_PROPOSAL_CHARS, n)
  assert.equal(n, 'spread_of_the_levels')
  assert.ok(bayesNameFromLabel('Supercalifragilisticexpialidocious').length <= BAYES_NAME_PROPOSAL_CHARS)
})

test('names are unique, never a function of the language, and fall back when nothing folds', () => {
  const taken = new Set(['school', 'school_2'])
  assert.equal(bayesUniqueName('School', taken), 'school_3')
  assert.equal(bayesUniqueName('exp', new Set()), 'exp_2', 'exp is a function')
  assert.equal(bayesUniqueName('Log', new Set()), 'log_2')
  assert.equal(bayesUniqueName('∑', new Set(), 'parameter'), 'parameter')
  assert.equal(bayesUniqueName('∑', new Set(['parameter']), 'parameter'), 'parameter_2')
  assert.ok(!bayesNameAllowed('a__'))
  assert.ok(!bayesNameAllowed('A'))
  assert.ok(bayesNameAllowed('a_b2'))
})

test('a confusable or an accented label gives a name the mirror accepts, unique in the graph', () => {
  const g = bayesOpenTemplate('hier_nested')
  const labels = ['Ѕcore', 'Ｓｃｏｒｅ', 'café', 'mu', 'u_d', 'Spread within a school', 'σ', 'Δ year', 'ﬁle']
  for (const label of labels) {
    const name = bayesUniqueName(label, bayesTakenNames(g), 'node')
    g.nodes.push({ id: `z${g.nodes.length}`, kind: 'parameter', name, label, prior: { dist: 'normal', args: { mu: { expr: '0' }, sigma: { expr: '1' } } } })
  }
  const model = bayesGraphModel(bayesGraphDecode(JSON.stringify(g)).graph)
  const nameFindings = model.findings.filter(f => f.code.startsWith('graph.name'))
  assert.deepEqual(nameFindings, [])
  const names = g.nodes.map(n => n.name).filter(Boolean)
  assert.equal(new Set(names).size, names.length)
  assert.equal(g.nodes.find(n => n.label === 'mu').name, 'mu_2', 'mu is taken by the template')
  assert.equal(g.nodes.find(n => n.label === 'Ｓｃｏｒｅ').name, 'score')
})

test('new ids: unique among plates, nodes and components, well formed', () => {
  const g = bayesOpenTemplate('hier_slopes')
  const a = bayesNewId(g, 'n')
  assert.match(a, /^[a-z][a-z0-9]{0,15}$/)
  g.nodes.push({ id: a, kind: 'data', label: 'x' })
  const b = bayesNewId(g, 'n')
  assert.notEqual(a, b)
  assert.notEqual(bayesNewId(g, 'p'), 'rows')
  assert.equal(bayesNewId({ plates: [{ id: 'p1' }], nodes: [] }, 'p'), 'p2')
})
