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

// ── the words (lot T2): labels, explanations and sources, in the five languages ──────────────────────

const LANGS = ['en', 'it', 'de', 'es', 'fr']
const LOCALE = Object.fromEntries(await Promise.all(LANGS.map(async (l) => [l, (await import(`../i18n/locales/${l}.gen.ts`)).default])))
const leafs = (o, p = '') => Object.entries(o).flatMap(([k, v]) => (typeof v === 'object' ? leafs(v, `${p}${k}.`) : [[`${p}${k}`, v]]))
const slots = (s) => [...new Set([...s.matchAll(/\{(\w+)\}/g)].map(m => m[1]))].sort()
// the judgement list of the preview's words, as the editor's words test has it
const VERDICTS = /\b(wide|narrow|vague|informative|reasonable|weak|strong|significant|plausible|good|bad|large|small|better|worse)\b|\b(ampio|ampia|stretto|vago|informativo|ragionevole|debole|forte|significativo|plausibile|peggio|meglio)\b|\b(breit|schmal|vage|informativ|vernünftig|schwach|stark|signifikant|plausibel|schlechter|besser)\b|\b(amplio|amplia|estrecho|vago|informativo|razonable|débil|fuerte|significativo|plausible|peor|mejor)\b|\b(large|étroit|vague|informatif|raisonnable|faible|fort|significatif|plausible)\b|less well|moins bien|meno bene/i

/** The keys of `labels` a template needs: its plates, its nodes (the AR's lags by their prefix), a correlated effect's components and correlation. */
function labelKeys (tpl) {
  const pre = tpl.order?.id_prefix
  const keys = tpl.graph.plates.map(p => `plate_${p.id}`)
  for (const n of tpl.graph.nodes) {
    if (pre && new RegExp(`^${pre}\\d+$`).test(n.id)) continue
    keys.push(n.id, ...(n.components ?? []).map(c => `${n.id}_${c.id}`), ...(n.correlation ? [`${n.id}_rho`] : []))
  }
  if (pre) keys.push(pre)
  return keys
}
/** The keys of `why`: every node that is not a lag of the AR (its prefix says them all) and every node's own words. */
const whyKeys = (tpl) => labelKeys(tpl).filter(k => !k.startsWith('plate_') && !/_(rho|c\w+)$/.test(k) || k === tpl.order?.id_prefix)

test('every template has its words in the five languages: data, a label for every box, a why for every node, the ways to change it', () => {
  for (const tpl of bayesTemplates()) {
    for (const lang of LANGS) {
      const t = LOCALE[lang].bayes.templates[tpl.id]
      const where = `${lang} ${tpl.id}`
      for (const k of ['title', 'what', 'data']) assert.ok(typeof t[k] === 'string' && t[k].length > 10, `${where} ${k}`)
      assert.deepEqual(Object.keys(t.labels).sort(), labelKeys(tpl).sort(), `${where}: labels`)
      assert.deepEqual(Object.keys(t.why).sort(), whyKeys(tpl).sort(), `${where}: why`)
      assert.ok(Object.keys(t.change).length >= 2, `${where}: change`)
      assert.ok(t.change.likelihood, `${where}: the likelihood is always changeable`)
      for (const [k, v] of leafs(t)) assert.ok(typeof v === 'string' && v.trim() === v && v.length > 0, `${where} ${k}`)
    }
    // the same keys everywhere
    const en = leafs(LOCALE.en.bayes.templates[tpl.id]).map(([k]) => k).sort()
    for (const lang of LANGS.slice(1)) assert.deepEqual(leafs(LOCALE[lang].bayes.templates[tpl.id]).map(([k]) => k).sort(), en, `${lang} ${tpl.id}`)
  }
})

test('the words say facts: the same placeholders in the five languages, no syntax of vue-i18n, no word that judges', () => {
  for (const tpl of bayesTemplates()) {
    const en = new Map(leafs(LOCALE.en.bayes.templates[tpl.id]))
    for (const lang of LANGS) {
      for (const [key, text] of leafs(LOCALE[lang].bayes.templates[tpl.id])) {
        const where = `${lang} ${tpl.id}.${key}`
        assert.deepEqual(slots(text), slots(en.get(key)), where)
        assert.doesNotMatch(text, /[|@]/, where)
        assert.ok(!/[{}]/.test(text.replace(/\{k\}/g, '')), `${where}: stray brace`)
        assert.doesNotMatch(text, VERDICTS, where)
      }
    }
  }
  // the only placeholder is the lag's number, in the AR's coefficient label
  const withSlot = bayesTemplates().flatMap(t => leafs(LOCALE.en.bayes.templates[t.id]).filter(([, v]) => slots(v).length).map(([k]) => `${t.id}.${k}`))
  assert.deepEqual(withSlot, ['ts_ar.labels.phi'])
})

test('opened in English, a template is its own file\'s graph, and in each language every label is that language\'s', () => {
  const textsOf = (lang, id) => Object.fromEntries(Object.entries(LOCALE[lang].bayes.templates[id].labels).map(([k, v]) => [`bayes.templates.${id}.labels.${k}`, v]))
  for (const tpl of bayesTemplates()) {
    for (const [name, options] of casesOf(tpl).slice(0, tpl.order ? 7 : 1)) {
      const plain = bayesOpenTemplate(tpl.id, options)
      assert.deepEqual(bayesOpenTemplate(tpl.id, options, textsOf('en', tpl.id)), plain, `${tpl.id} ${name}: the English words are the file's`)
      for (const lang of LANGS.slice(1)) {
        const g = bayesOpenTemplate(tpl.id, options, textsOf(lang, tpl.id))
        check(g)
        const texts = textsOf(lang, tpl.id)
        const said = (key) => texts[`bayes.templates.${tpl.id}.labels.${key}`]
        for (const n of g.nodes) {
          const lag = tpl.order && new RegExp(`^${tpl.order.id_prefix}\\d+$`).test(n.id)
          const want = lag ? said(tpl.order.id_prefix).replace('{k}', n.id.slice(tpl.order.id_prefix.length)) : said(n.id)
          assert.equal(n.label, want, `${lang} ${tpl.id} ${n.id}`)
        }
        for (const p of g.plates) assert.equal(p.label, said(`plate_${p.id}`), `${lang} ${tpl.id} ${p.id}`)
      }
    }
  }
  // few words are the same as English's (a cognate such as «Predictor»): never most of a template's
  for (const tpl of bayesTemplates()) {
    const en = LOCALE.en.bayes.templates[tpl.id].labels
    for (const lang of LANGS.slice(1)) {
      const same = Object.entries(LOCALE[lang].bayes.templates[tpl.id].labels).filter(([k, v]) => v === en[k]).length
      assert.ok(same <= 3, `${lang} ${tpl.id}: ${same} labels left in English`)
    }
  }
})

test('a node with a prior or a first value says its default as the file has it, and its source', () => {
  const SOURCE = { rstanarm_priors: /rstanarm/, stan_prior_wiki: /Stan/, spec: /Tabularia/ }
  const lagOf = (tpl, id) => (tpl.order && new RegExp(`^${tpl.order.id_prefix}\\d+$`).test(id) ? tpl.order.id_prefix : id)
  for (const tpl of bayesTemplates()) {
    for (const n of tpl.graph.nodes) {
      const id = lagOf(tpl, n.id)
      const slot = n.prior ?? n.init
      const args = slot ? Object.values(slot.args ?? {}).map(a => a.expr) : []
      for (const lang of LANGS) {
        const why = LOCALE[lang].bayes.templates[tpl.id].why[id]
        const where = `${lang} ${tpl.id} ${n.id}`
        // a distribution with numbers only (the group-level draws and the correlated effects have their own sentence)
        if (slot && args.every(a => /^[\d.]+$/.test(a))) assert.ok(why.includes(`${slot.dist}(${args.join(', ')})`), `${where}: the default ${slot.dist}(${args.join(', ')}) is not said`)
        if (n.correlation) assert.ok(why.includes(`LKJ(${n.correlation.eta.expr})`), `${where}: the correlation's default`)
        const src = tpl.sources?.[n.id] ?? tpl.sources?.[id]
        if (src) assert.match(why, SOURCE[src], `${where}: source ${src}`)
      }
    }
    // every source named by a node is a node of the template or the AR's first lag
    for (const k of Object.keys(tpl.sources ?? {})) assert.ok(tpl.graph.nodes.some(n => n.id === k), `${tpl.id}: source of ${k}`)
  }
})
