// The model graph's browser mirror (composables/bayesGraph.ts) against the gateway's validator:
// every case of the mirror corpus (gateway-go/internal/bayesgraph/testdata/mirror/cases.json,
// written by the Go test TestMirrorCorpus) gives the same findings (code, severity, node, slot,
// arguments, in the same order) and, for a graph that decodes, the same order, parameters, edges,
// scale pairs and lags. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const {
  bayesGraphDecode, bayesGraphModel, bayesGraphMessageKey, bayesPriorPlan, bayesGraphValidate,
} = await import('~/composables/bayesGraph.ts')
const { default: en } = await import('../i18n/locales/en.gen.ts')
const { default: it } = await import('../i18n/locales/it.gen.ts')
const { default: de } = await import('../i18n/locales/de.gen.ts')
const { default: es } = await import('../i18n/locales/es.gen.ts')
const { default: fr } = await import('../i18n/locales/fr.gen.ts')

const GO_DIR = new URL('../../gateway-go/internal/bayesgraph/testdata/', import.meta.url)
const MIRROR = fileURLToPath(new URL('mirror/cases.json', GO_DIR))
const CORPUS_DIR = fileURLToPath(new URL('corpus/', GO_DIR))
const LANGUAGES = { en, it, de, es, fr }

/** A finding as a comparison sees it: no undefined, a missing node, slot or arguments are empty; the text of a decoder's error is Go's own. */
function norm (f) {
  const args = JSON.parse(JSON.stringify(f.args ?? {}))
  if (f.code === 'graph.format') delete args.detail
  return { code: f.code, severity: f.severity, node: f.node ?? '', slot: f.slot ?? '', args }
}

function run (input) {
  const { graph, findings } = bayesGraphDecode(input)
  if (!graph) return { decoded: false, findings, model: null }
  const model = bayesGraphModel(graph)
  return { decoded: true, findings: model.findings, model }
}

const num = x => (x === Infinity ? 'Infinity' : x === -Infinity ? '-Infinity' : x)
const iv = i => [num(i.lo), num(i.hi), i.loOpen, i.hiOpen]

test('the mirror gives the gateway\'s findings and model on every case of the corpus', (t) => {
  if (!existsSync(MIRROR)) return t.skip('the gateway\'s mirror corpus is not in this checkout')
  const cases = JSON.parse(readFileSync(MIRROR, 'utf8')).cases
  assert.ok(cases.length > 300, `${cases.length} cases`)
  const codes = new Set()
  for (const c of cases) {
    const got = run(c.input)
    assert.equal(got.decoded, c.decoded, `${c.id}: decoded`)
    assert.deepEqual(got.findings.map(norm), c.findings.map(norm), `${c.id}: findings`)
    for (const f of c.findings) codes.add(f.code)
    if (!c.decoded) continue
    const m = got.model
    assert.deepEqual(m.order, c.model.order, `${c.id}: order`)
    assert.deepEqual(m.lags, c.model.lags, `${c.id}: lags`)
    assert.deepEqual(
      m.edges.map(e => [e.from, e.to, e.node, e.slot, e.kind]), c.model.edges, `${c.id}: edges`)
    assert.deepEqual(m.pairs.map(p => [p.node, p.term, p.data, p.intercept]), c.model.pairs, `${c.id}: pairs`)
    const params = {}
    for (const [id, p] of m.params) {
      params[id] = {
        dist: p.dist, support: iv(p.support), range: iv(p.range), truncated: p.truncated,
        truncate_in_program: p.truncateInProgram, role: p.role, non_centred: p.nonCentred,
      }
    }
    assert.deepEqual(params, c.model.params, `${c.id}: params`)
  }
  for (const code of ['graph.format', 'graph.cycle', 'graph.prior_data', 'graph.likelihood', 'graph.terms', 'graph.state', 'graph.effects', 'graph.slot_maybe']) {
    assert.ok(codes.has(code), code)
  }
})

test('the mirror reads the shared corpus of the validator too, when it is there', (t) => {
  if (!existsSync(CORPUS_DIR)) return t.skip('the gateway\'s corpus is not in this checkout')
  let n = 0
  for (const name of readdirSync(CORPUS_DIR).filter(f => f.endsWith('.json')).sort()) {
    const c = JSON.parse(readFileSync(CORPUS_DIR + name, 'utf8'))
    let input
    if (c.raw !== undefined && c.raw !== null) input = c.raw
    else if (c.input !== undefined) input = JSON.stringify(c.input)
    else continue // a big case the Go test builds itself
    const got = run(input)
    assert.deepEqual(got.findings.map(norm), (c.findings ?? []).map(norm), `${name}`)
    n++
  }
  assert.ok(n > 100, `${n} cases`)
})

test('a document over the limits is refused as the gateway does', () => {
  const big = 'x'.repeat(262145)
  const r = bayesGraphDecode(`{"format_version":1,"plates":[],"nodes":[],"layout":{"a":"${big}"}}`)
  assert.equal(r.graph, null)
  assert.deepEqual(r.findings.map(norm).map(f => [f.code, f.args.what, f.args.max]), [['graph.too_big', 'bytes', 262144]])
  const nodes = Array.from({ length: 201 }, (_, i) => ({ id: `a${i}`, kind: 'transformed', label: 'A', value: { expr: '1' } }))
  const r2 = bayesGraphDecode(JSON.stringify({ format_version: 1, plates: [], nodes }))
  assert.deepEqual(r2.findings.map(norm).map(f => [f.code, f.args.what, f.args.got, f.args.max]), [['graph.too_big', 'nodes', 201, 200]])
  const plates = Array.from({ length: 9 }, (_, i) => ({ id: `p${i}`, kind: 'group', label: 'P' }))
  const r3 = bayesGraphDecode(JSON.stringify({ format_version: 1, plates, nodes: [] }))
  assert.deepEqual(r3.findings.map(norm).map(f => [f.code, f.args.what, f.args.got, f.args.max]), [['graph.too_big', 'plates', 9, 8]])
})

test('the mirror never builds a canonical form or a hash', async () => {
  const mod = await import('~/composables/bayesGraph.ts')
  for (const name of Object.keys(mod)) assert.ok(!/canonical|hash/i.test(name), name)
})

test('a graph checked twice gives the same answer (the validation changes nothing)', () => {
  const cases = existsSync(MIRROR) ? JSON.parse(readFileSync(MIRROR, 'utf8')).cases.filter(c => c.decoded).slice(0, 40) : []
  for (const c of cases) {
    const g = bayesGraphDecode(c.input).graph
    const before = JSON.stringify(g)
    const a = bayesGraphValidate(g)
    const b = bayesGraphValidate(g)
    assert.deepEqual(a, b, c.id)
    assert.equal(JSON.stringify(g), before, c.id)
  }
})

// ---------------------------------------------------------------- the prior plan

function planOf (id) {
  const cases = JSON.parse(readFileSync(MIRROR, 'utf8')).cases
  const c = cases.find(x => x.id === id)
  assert.ok(c, id)
  return bayesPriorPlan(run(c.input).model)
}

test('the plan holds the parameters and the values that need no data, ancestors first', (t) => {
  if (!existsSync(MIRROR)) return t.skip('the gateway\'s mirror corpus is not in this checkout')
  // regression: the four parameters; the linear predictor and the likelihood read data
  const r = planOf('base-regression')
  assert.deepEqual(r.order, ['m1', 'm2', 'm3', 'm4'])
  assert.deepEqual(r.nodes.m1.args.sigma, { constant: 10 })
  assert.equal(r.nodes.m4.dist, 'exponential')
  assert.deepEqual(r.nodes.m4.range, { lower: 0 })
  assert.equal(r.nodes.m4.truncated, false)
  assert.ok(Object.values(r.nodes).every(n => n.errors === 0))
  // the hierarchy: a lifted index in a prior, a levels plate, a transformed value, bounds
  const h = planOf('base-rich')
  for (const id of ['m4', 'm5', 'm6', 'm9', 'm10', 't1']) assert.ok(h.nodes[id], id)
  assert.ok(h.order.indexOf('m4') < h.order.indexOf('m5'), 'mu_d before a_s')
  assert.ok(h.order.indexOf('m1') < h.order.indexOf('m4'))
  assert.ok(h.order.indexOf('m5') < h.order.indexOf('t1') || h.order.indexOf('m4') < h.order.indexOf('t1'))
  assert.equal(h.nodes.m5.plate, 'p2')
  assert.equal(h.nodes.m6.plate, 'levels:d5')
  assert.deepEqual(h.nodes.m5.args.mu, { ast: { kind: 'index', id: 'm4', by: 'd4', lifted: true } })
  assert.deepEqual(h.nodes.m2.range, { lower: 0 })
  assert.equal(h.nodes.m2.truncated, true)
  assert.deepEqual(h.nodes.m10.range, { lower: 0, upper: 0.5 })
  assert.equal(h.nodes.m10.truncated, true)
  assert.deepEqual(h.nodes.m9.range, { lower: 0, upper: 1 })
  assert.equal(h.nodes.t1.kind, 'transformed')
  assert.equal(h.nodes.t1.value.ast.kind, 'binary')
  for (const id of ['p3', 'l1', 'l2', 'd1']) assert.equal(h.nodes[id], undefined, id)
  // correlated effects: components and correlation; the transformed value reads the data
  const l = planOf('base-logistic')
  assert.deepEqual(l.order, ['m1', 'm2', 'm3', 'm4', 'e1.c1', 'e1.c2', 'e1.correlation'])
  assert.equal(l.nodes['e1.c1'].kind, 'component')
  assert.deepEqual(l.nodes['e1.c1'].args.mu, { ast: { kind: 'name', id: 'm1' } })
  assert.deepEqual(l.nodes['e1.c2'].args.mu, { ref: 'm2' })
  assert.deepEqual(l.nodes['e1.correlation'], { id: 'e1.correlation', kind: 'correlation', plate: 'p1', dist: 'lkj_corr_cholesky', k: 2, eta: 2, errors: 0 })
  // states: the first value and the scale
  const s = planOf('base-locallevel')
  assert.deepEqual(s.order, ['m1', 'm2', 's1.init', 's1.scale'])
  assert.equal(s.nodes['s1.init'].kind, 'state_init')
  assert.equal(s.nodes['s1.scale'].kind, 'state_scale')
  assert.deepEqual(s.nodes['s1.scale'].value, { ast: { kind: 'name', id: 'm1' } })
})

test('the plan counts the errors on a node or an ancestor, and holds no data and no label', (t) => {
  if (!existsSync(MIRROR)) return t.skip('the gateway\'s mirror corpus is not in this checkout')
  const g = JSON.parse(JSON.parse(readFileSync(MIRROR, 'utf8')).cases.find(x => x.id === 'base-example').input)
  g.nodes.find(n => n.id === 'n4').prior.args.sigma = { expr: '-1' } // mu: an error on a parameter
  const plan = bayesPriorPlan(run(JSON.stringify(g)).model)
  assert.equal(plan.nodes.n4.errors, 1)
  assert.equal(plan.nodes.n6.errors, 1, 'a child of the one with the error')
  assert.equal(plan.nodes.n7.errors, 0)
  assert.ok(plan.order.indexOf('n4') < plan.order.indexOf('n6'))
  const text = JSON.stringify(plan)
  for (const label of ['Mean of the schools', 'Test score', 'Hours of study']) assert.ok(!text.includes(label), label)
  // a label error does not stop a prior being drawn
  const h = JSON.parse(JSON.stringify(g))
  h.nodes.find(n => n.id === 'n4').prior.args.sigma = { expr: '10' }
  h.nodes.find(n => n.id === 'n4').label = ''
  assert.equal(bayesPriorPlan(run(JSON.stringify(h)).model).nodes.n4.errors, 0)
  // a cycle: its nodes and their readers are not planned (graph.cycle says why)
  const c = JSON.parse(JSON.stringify(g))
  c.nodes.find(n => n.id === 'n4').prior.args.mu = { expr: 'tau' }
  c.nodes.find(n => n.id === 'n5').prior.args.mu = { expr: 'mu' }
  const cp = bayesPriorPlan(run(JSON.stringify(c)).model)
  assert.ok(cp.nodes.n4 === undefined && cp.nodes.n5 === undefined && cp.nodes.n6 === undefined)
  assert.ok(cp.nodes.n7)
  // nothing is planned for a structure that cannot be trusted
  assert.deepEqual(bayesPriorPlan(run('{"format_version":1,"plates":[],"nodes":[]}').model), { order: [], nodes: {} })
})

// ---------------------------------------------------------------- the messages

const placeholders = text => [...text.matchAll(/\{([a-z_]+)\}/g)].map(m => m[1])

test('every finding of the corpus has its message in the five languages, with no syntax of vue-i18n', (t) => {
  if (!existsSync(MIRROR)) return t.skip('the gateway\'s mirror corpus is not in this checkout')
  const cases = JSON.parse(readFileSync(MIRROR, 'utf8')).cases
  const keys = new Map()
  for (const c of cases) {
    for (const f of c.findings) {
      const key = bayesGraphMessageKey(f)
      if (key === null) { assert.ok(f.code.startsWith('expr.'), f.code); continue }
      if (!keys.has(key)) keys.set(key, [])
      keys.get(key).push(f)
    }
  }
  assert.ok(keys.size > 80, `${keys.size} message keys`)
  for (const [key, fs] of keys) {
    for (const [lang, tree] of Object.entries(LANGUAGES)) {
      const msg = key.split('.').reduce((o, k) => (o ? o[k] : undefined), tree.bayes.graph)
      assert.equal(typeof msg, 'string', `${lang}: ${key}`)
      assert.ok(msg.length > 10, `${lang}: ${key}`)
      assert.ok(!/[|@]/.test(msg), `${lang}: ${key} has | or @`)
      assert.ok(!/[{}]/.test(msg.replace(/\{[a-z_]+\}/g, '')), `${lang}: ${key} has a stray brace`)
      for (const p of placeholders(msg)) {
        for (const f of fs) assert.ok(f.args && p in f.args, `${lang}: ${key} asks for {${p}}, a finding of ${f.code} on ${f.node} has ${JSON.stringify(f.args)}`)
      }
    }
  }
})

test('the five languages say the same things: the same keys and the same placeholders', () => {
  const flat = (o, prefix = '') => Object.entries(o).flatMap(([k, v]) => (typeof v === 'string' ? [[prefix + k, v]] : flat(v, `${prefix}${k}.`)))
  const base = new Map(flat(en.bayes.graph))
  assert.ok(base.size > 100)
  for (const [lang, tree] of Object.entries(LANGUAGES)) {
    const own = new Map(flat(tree.bayes.graph))
    assert.deepEqual([...own.keys()].sort(), [...base.keys()].sort(), lang)
    for (const [k, v] of own) assert.deepEqual(placeholders(v).sort(), placeholders(base.get(k)).sort(), `${lang}: ${k}`)
  }
})
