// The templates of the Bayesian editor without a browser (composables/bayesTemplates.ts): the copy is
// the gateway's file, every template (and every one opened with the outcome's numbers or an AR order)
// is a graph the mirror finds no error and no warning in, and bayesOpenTemplate gives the opened graphs
// the gateway's tests read (gateway-go/internal/bayestemplates/testdata/opened/<id>.<case>.json).
// Rewrite the fixtures (reviewed by hand) with: BAYES_TEMPLATES_WRITE=1 npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const { BAYES_TEMPLATES, bayesTemplates, bayesTemplate, bayesOpenTemplate, bayesTemplateMaxOrder } = await import('~/composables/bayesTemplates.ts')
const { bayesGraphDecode, bayesGraphModel } = await import('~/composables/bayesGraph.ts')
const { bayesLimit } = await import('~/composables/bayesCatalog.ts')

const GO_DIR = new URL('../../gateway-go/internal/bayestemplates/', import.meta.url)
const OPENED = fileURLToPath(new URL('testdata/opened/', GO_DIR))
const WRITE = process.env.BAYES_TEMPLATES_WRITE === '1'

const IDS = ['glm_linear', 'glm_logistic', 'glm_poisson', 'glm_negbin', 'hier_intercepts', 'hier_slopes',
  'hier_nested', 'ts_ar', 'ts_random_walk', 'ts_local_level']
const MAX_LAG = bayesLimit('max_lag')
const MAX_ORDER = bayesTemplateMaxOrder(bayesTemplate('ts_ar'))

// The cases a template is opened with: the fixtures' names and the options.
const SCALES = [
  ['default', {}],
  ['c0-s1', { center: 0, spread: 1 }],
  ['c1200-s300', { center: 1200, spread: 300 }],
  ['cm5-s0p02', { center: -5, spread: 0.02 }],
  ['s300', { spread: 300 }],
  ['c1200', { center: 1200 }],
]
const ORDERS = [1, 2, 5, MAX_ORDER].map(p => [`p${p}`, { order: p }])
const casesOf = (tpl) => {
  const cases = tpl.scale === 'outcome' ? [...SCALES] : [['default', {}]]
  if (tpl.order) cases.push(...ORDERS, ['p5-c1200-s300', { order: 5, center: 1200, spread: 300 }])
  return cases
}

function check (graph) {
  const { graph: g, findings } = bayesGraphDecode(JSON.stringify(graph))
  assert.deepEqual(findings, [], 'decodes')
  const model = bayesGraphModel(g)
  assert.deepEqual(model.findings, [], 'no error and no warning')
  return model
}

test('the copy is the gateway\'s file, byte for byte', () => {
  const go = readFileSync(fileURLToPath(new URL('templates.json', GO_DIR)))
  const copy = readFileSync(fileURLToPath(new URL('../assets/bayes-templates.json', import.meta.url)))
  assert.ok(go.equals(copy))
})

test('the ten templates, in the spec\'s order, with a version and the file\'s own keys', () => {
  assert.deepEqual(bayesTemplates().map(t => t.id), IDS)
  for (const t of bayesTemplates()) {
    assert.ok(Number.isInteger(t.version) && t.version >= 1, t.id)
    for (const key of Object.values(t.sources)) assert.ok(key === 'spec' || key in BAYES_TEMPLATES.sources, `${t.id}: ${key}`)
    const ids = new Set(t.graph.nodes.map(n => n.id))
    for (const s of t.scaled) assert.ok(ids.has(s.node), `${t.id}: ${s.node}`)
  }
})

for (const id of IDS) {
  const tpl = bayesTemplate(id)
  test(`${id}: opened with each case, the mirror finds nothing and the graph is the fixture`, () => {
    for (const [name, options] of casesOf(tpl)) {
      const graph = bayesOpenTemplate(id, options)
      check(graph)
      const path = `${OPENED}${id}.${name}.json`
      const text = JSON.stringify(graph, null, 2) + '\n'
      if (WRITE) {
        mkdirSync(OPENED, { recursive: true })
        writeFileSync(path, text)
      }
      assert.ok(existsSync(path), `${path} is missing: BAYES_TEMPLATES_WRITE=1 npm test`)
      assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), graph, `${id}.${name}`)
    }
  })
}

test('every fixture is a case of a template', () => {
  const wanted = new Set(bayesTemplates().flatMap(t => casesOf(t).map(([n]) => `${t.id}.${n}.json`)))
  assert.deepEqual(readdirSync(OPENED).sort(), [...wanted].sort())
})

test('no option: the template\'s graph, a deep copy', () => {
  for (const t of bayesTemplates()) {
    const g = bayesOpenTemplate(t.id)
    assert.deepEqual(g, t.graph)
    assert.notStrictEqual(g, t.graph)
    g.nodes[0].label = 'changed'
    assert.notEqual(bayesTemplate(t.id).graph.nodes[0].label, 'changed')
  }
})

test('the outcome\'s numbers are written in the open', () => {
  const slot = (g, id, part, arg) => g.nodes.find(n => n.id === id)[part].args[arg].expr
  const g = bayesOpenTemplate('glm_linear', { center: 1200, spread: 300 })
  assert.equal(slot(g, 'alpha', 'prior', 'mu'), '1200')
  assert.equal(slot(g, 'alpha', 'prior', 'sigma'), '2.5 * 300')
  assert.equal(slot(g, 'bx', 'prior', 'sigma'), '2.5 * 300')
  assert.equal(slot(g, 'sigma', 'prior', 'beta'), '1 / 300')
  const h = bayesOpenTemplate('glm_linear', { center: -5, spread: 0.02 })
  assert.equal(slot(h, 'alpha', 'prior', 'mu'), '-5')
  assert.equal(slot(h, 'sigma', 'prior', 'beta'), '1 / 0.02')
  const t = bayesOpenTemplate('hier_intercepts', { spread: 300 })
  assert.equal(slot(t, 'taua', 'prior', 'sigma'), '300')
  assert.equal(slot(t, 'mua', 'prior', 'mu'), '0')
  const l = bayesOpenTemplate('ts_local_level', { center: 10, spread: 4 })
  assert.equal(slot(l, 'level', 'init', 'mu'), '10')
  assert.equal(slot(l, 'level', 'init', 'sigma'), '2.5 * 4')
  assert.equal(slot(l, 'sigmalevel', 'prior', 'sigma'), '4')
  // 0 and 1 are what a person who gives nothing gets
  assert.deepEqual(bayesOpenTemplate('glm_linear', { center: 0, spread: 1 }), bayesOpenTemplate('glm_linear'))
  // the templates whose priors are not on the outcome's scale ignore the numbers
  assert.deepEqual(bayesOpenTemplate('glm_logistic', { center: 5, spread: 9 }), bayesOpenTemplate('glm_logistic'))
})

test('the AR order: the coefficients and the lags of the likelihood', () => {
  const lik = (g) => g.nodes.find(n => n.id === 'lik').args.mu.expr
  assert.equal(lik(bayesOpenTemplate('ts_ar')), 'mu + phi1 * (lag(y, 1) - mu) + phi2 * (lag(y, 2) - mu)')
  assert.deepEqual(bayesOpenTemplate('ts_ar', { order: 2 }), bayesOpenTemplate('ts_ar'))
  assert.equal(lik(bayesOpenTemplate('ts_ar', { order: 1 })), 'mu + phi1 * (lag(y, 1) - mu)')
  const g5 = bayesOpenTemplate('ts_ar', { order: 5 })
  assert.equal(lik(g5), 'mu + phi1 * (lag(y, 1) - mu) + phi2 * (lag(y, 2) - mu) + phi3 * (lag(y, 3) - mu) + phi4 * (lag(y, 4) - mu) + phi5 * (lag(y, 5) - mu)')
  assert.deepEqual(g5.nodes.filter(n => n.id.startsWith('phi')).map(n => n.name), ['phi1', 'phi2', 'phi3', 'phi4', 'phi5'])
  assert.equal(g5.nodes.find(n => n.id === 'phi5').label, 'Coefficient of lag 5')
  const model = check(bayesOpenTemplate('ts_ar', { order: MAX_ORDER }))
  assert.deepEqual(model.lags, Array.from({ length: MAX_ORDER }, (_, i) => i + 1))
  // the largest order is the one whose mean still fits the language's limits, never above max_lag
  assert.ok(MAX_ORDER >= 5 && MAX_ORDER <= MAX_LAG, String(MAX_ORDER))
  assert.throws(() => bayesOpenTemplate('ts_ar', { order: MAX_ORDER + 1 }), RangeError)
  assert.equal(bayesTemplateMaxOrder(bayesTemplate('glm_linear')), 0)
})

test('the layout: no two nodes overlap, at any order', () => {
  const W = 180
  const H = 80
  for (const [id, options] of [...IDS.map(i => [i, {}]), ['ts_ar', { order: 1 }], ['ts_ar', { order: 5 }], ['ts_ar', { order: 9 }]]) {
    const g = bayesOpenTemplate(id, options)
    const boxes = g.nodes.map(n => ({ id: n.id, ...g.layout[n.id] }))
    for (const b of boxes) {
      assert.ok(Number.isFinite(b.x) && Number.isFinite(b.y), `${id}: ${b.id} has no place`)
      assert.ok(b.x >= -20 && b.x + W <= 1280, `${id}: ${b.id} outside 1280`)
    }
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i]
        const c = boxes[j]
        const apart = a.x + W <= c.x || c.x + W <= a.x || a.y + H <= c.y || c.y + H <= a.y
        assert.ok(apart, `${id}${JSON.stringify(options)}: ${a.id} and ${c.id} overlap`)
      }
    }
  }
})

// The canvas reads left to right (docs/specs/bayes-graph-editor.md, «Edges»): an edge leaves its
// source's right handle and enters its target's left one, so every source stands wholly to the left
// of its target, at every order of the AR template and with the outcome's numbers.
test('the layout: every edge goes rightwards, at any order', () => {
  const W = 180
  const owner = (end) => end.split('.')[0]
  for (const tpl of bayesTemplates()) {
    const cases = [...casesOf(tpl), ...(tpl.order ? [['p9', { order: 9 }]] : [])]
    for (const [name, options] of cases) {
      const g = bayesOpenTemplate(tpl.id, options)
      const model = check(g)
      assert.ok(model.edges.length > 0, `${tpl.id}.${name}: no edge`)
      for (const e of model.edges) {
        const from = g.layout[owner(e.from)]
        const to = g.layout[owner(e.to)]
        assert.ok(from && to, `${tpl.id}.${name}: ${e.from} → ${e.to} has no place`)
        assert.ok(from.x + W <= to.x, `${tpl.id}.${name}: ${e.from} (x ${from.x}) → ${e.to} (x ${to.x}) does not go rightwards`)
      }
    }
  }
})

test('the layout: the rows and every group plate have a box, and each node sits in its own', () => {
  const W = 180
  const H = 80
  for (const tpl of bayesTemplates()) {
    const g = bayesOpenTemplate(tpl.id, tpl.order ? { order: 5 } : {})
    const observedPlate = new Map(g.nodes.map(n => [n.id, n.plate ?? null]))
    for (const p of g.plates) {
      const box = g.layout[p.id]
      assert.ok(box && box.w > 0 && box.h > 0, `${tpl.id}: the plate ${p.id} has no box`)
    }
    for (const n of g.nodes) {
      const at = g.layout[n.id]
      const plate = n.kind === 'likelihood' ? observedPlate.get(n.observed) : (n.plate ?? null)
      if (plate === null) continue
      const box = g.layout[plate]
      const inside = at.x >= box.x && at.x + W <= box.x + box.w && at.y >= box.y && at.y + H <= box.y + box.h
      assert.ok(inside, `${tpl.id}: ${n.id} is not inside the box of ${plate}`)
    }
  }
})

test('the labels come from the person\'s language, English where a key is missing', () => {
  const texts = { 'bayes.templates.ts_ar.labels.mu': 'Livello', 'bayes.templates.ts_ar.labels.phi': 'Coefficiente del ritardo {k}', 'bayes.templates.ts_ar.labels.plate_rows': 'Mesi' }
  const g = bayesOpenTemplate('ts_ar', { order: 3 }, texts)
  assert.equal(g.nodes.find(n => n.id === 'mu').label, 'Livello')
  assert.equal(g.nodes.find(n => n.id === 'phi3').label, 'Coefficiente del ritardo 3')
  assert.equal(g.plates[0].label, 'Mesi')
  assert.equal(g.nodes.find(n => n.id === 'sigma').label, bayesTemplate('ts_ar').graph.nodes.find(n => n.id === 'sigma').label)
  const fn = (key) => (key === 'bayes.templates.hier_slopes.labels.eff_ca' ? 'Livello di un gruppo' : key === 'bayes.templates.hier_slopes.labels.eff_rho' ? 'Correlazione' : undefined)
  const s = bayesOpenTemplate('hier_slopes', {}, fn)
  const eff = s.nodes.find(n => n.id === 'eff')
  assert.equal(eff.components[0].label, 'Livello di un gruppo')
  assert.equal(eff.correlation.label, 'Correlazione')
  check(g)
})

test('numbers out of range are refused', () => {
  assert.throws(() => bayesOpenTemplate('ts_ar', { order: 0 }), RangeError)
  assert.throws(() => bayesOpenTemplate('ts_ar', { order: MAX_LAG + 1 }), RangeError)
  assert.throws(() => bayesOpenTemplate('ts_ar', { order: 1.5 }), RangeError)
  assert.throws(() => bayesOpenTemplate('glm_linear', { spread: 0 }), RangeError)
  assert.throws(() => bayesOpenTemplate('glm_linear', { spread: -1 }), RangeError)
  assert.throws(() => bayesOpenTemplate('glm_linear', { center: Number.NaN }), RangeError)
  assert.throws(() => bayesOpenTemplate('glm_linear', { center: 1e20 }), RangeError)
  assert.throws(() => bayesOpenTemplate('nope'), /no template/)
})
