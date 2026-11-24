// The prior preview's worker and the page's side (composables/bayesPrior.worker.ts and
// composables/bayesPriorPreview.ts): the worker answers a request with transferable arrays and never
// throws; the client debounces, sends one request at a time, shows only its latest answer and drops
// what was cancelled; the words exist in the five languages and say no verdict; and nothing the entry
// chunk can reach imports the core or stdlib (the build check, `nuxt build`, confirms it on the chunks).
// Small data always: a plan of one or two nodes.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const { bayesPriorPreviewClient } = await import('~/composables/bayesPriorPreview.ts')

import enBase from '../i18n/locales/en.ts'
import enGen from '../i18n/locales/en.gen.ts'
import itGen from '../i18n/locales/it.gen.ts'
import deGen from '../i18n/locales/de.gen.ts'
import esGen from '../i18n/locales/es.gen.ts'
import frGen from '../i18n/locales/fr.gen.ts'

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..')

const normalPlan = {
  order: ['mu'],
  nodes: { mu: { id: 'mu', kind: 'parameter', plate: '', errors: 0, dist: 'normal', args: { mu: { constant: 0 }, sigma: { constant: 1 } } } },
}

// ---------------------------------------------------------------- the worker

test('the worker answers a request with its id and the arrays to transfer', async () => {
  const sent = []
  globalThis.self = { postMessage: (message, transfer) => sent.push({ message, transfer }) }
  await import('~/composables/bayesPrior.worker.ts')
  assert.equal(typeof self.onmessage, 'function')
  self.onmessage({ data: { id: 7, plan: normalPlan, target: 'mu', seed: 1 } })
  assert.equal(sent.length, 1)
  const { message, transfer } = sent[0]
  assert.equal(message.id, 7)
  assert.equal(message.answer.kind, 'curve')
  assert.ok(message.answer.x instanceof Float64Array)
  assert.deepEqual(transfer, [message.answer.x.buffer, message.answer.y.buffer])
  // the answer of a quantity the plan does not hold is an answer, not an exception
  self.onmessage({ data: { id: 8, plan: normalPlan, target: 'missing', seed: 1 } })
  assert.equal(sent[1].message.answer.none, 'needs_data')
  assert.deepEqual(sent[1].transfer, [])
})

test('a request the core cannot take is answered as failed, never thrown', async () => {
  const sent = []
  const quiet = console.error
  console.error = () => {}
  try {
    globalThis.self.postMessage = (message) => sent.push(message)
    self.onmessage({ data: { id: 9, plan: null, target: 'mu', seed: 1 } })
  } finally { console.error = quiet }
  assert.deepEqual(sent, [{ id: 9, failed: true }])
})

// ---------------------------------------------------------------- the page's client

function fakeWorker () {
  const worker = {
    sent: [], terminated: false, onmessage: null, onerror: null,
    postMessage (message) { worker.sent.push(message) },
    terminate () { worker.terminated = true },
    answer (id, extra = {}) { worker.onmessage({ data: { id, answer: { target: 't', kind: 'curve', ms: 1, ...extra } } }) },
  }
  return worker
}
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms))

function harness (delay = 10) {
  const worker = fakeWorker()
  const states = []
  let starts = 0
  const client = bayesPriorPreviewClient(async () => { starts++; return worker }, s => states.push(s), delay)
  return { worker, states, client, starts: () => starts }
}

test('the worker is not started until a chart is asked for, and then once', async () => {
  const h = harness()
  assert.equal(h.starts(), 0)
  h.client.request(normalPlan, 'mu', 1)
  await wait(40)
  h.worker.answer(1)
  h.client.request(normalPlan, 'mu', 2)
  await wait(40)
  assert.equal(h.starts(), 1)
  assert.equal(h.worker.sent.length, 2)
  h.client.dispose()
  assert.ok(h.worker.terminated)
})

test('changes within the quiet time make one request, the latest', async () => {
  const h = harness(30)
  for (const seed of [1, 2, 3]) { h.client.request(normalPlan, 'mu', seed); await wait(5) }
  assert.equal(h.worker.sent.length, 0)
  await wait(80)
  assert.equal(h.worker.sent.length, 1)
  assert.equal(h.worker.sent[0].seed, 3)
  assert.equal(h.states.at(-1).pending, true)
  h.worker.answer(h.worker.sent[0].id, { target: 'mu' })
  assert.equal(h.states.at(-1).pending, false)
  assert.equal(h.states.at(-1).answer.target, 'mu')
  h.client.dispose()
})

test('one request at a time: a newer one waits for the answer in flight, whose answer is not shown', async () => {
  const h = harness()
  h.client.request(normalPlan, 'mu', 1)
  await wait(40)
  h.client.request(normalPlan, 'mu', 2)
  await wait(40)
  assert.equal(h.worker.sent.length, 1, 'the worker is busy')
  h.worker.answer(h.worker.sent[0].id, { target: 'old' })
  assert.equal(h.worker.sent.length, 2)
  assert.equal(h.worker.sent[1].seed, 2)
  assert.equal(h.states.at(-1).answer, null, 'the stale answer is not shown')
  h.worker.answer(h.worker.sent[1].id, { target: 'new' })
  assert.equal(h.states.at(-1).answer.target, 'new')
  h.client.dispose()
})

test('an answer with another id than the one in flight is ignored', async () => {
  const h = harness()
  h.client.request(normalPlan, 'mu', 1)
  await wait(40)
  h.worker.answer(99)
  assert.equal(h.states.at(-1).answer, null)
  assert.equal(h.states.at(-1).pending, true)
  h.client.dispose()
})

test('a cancelled request is dropped, in flight or waiting, and the client works again after', async () => {
  const h = harness()
  h.client.request(normalPlan, 'mu', 1)
  h.client.cancel()
  await wait(40)
  assert.equal(h.worker.sent.length, 0)
  h.client.request(normalPlan, 'mu', 2)
  await wait(40)
  h.client.cancel()
  h.worker.answer(h.worker.sent[0].id, { target: 'cancelled' })
  assert.equal(h.states.at(-1).answer, null)
  assert.equal(h.states.at(-1).pending, false)
  h.client.request(normalPlan, 'mu', 3)
  await wait(40)
  h.worker.answer(h.worker.sent[1].id, { target: 'after' })
  assert.equal(h.states.at(-1).answer.target, 'after', 'the dropped answer did not eat the next one')
  h.client.dispose()
})

test('a failure is a state, the next request works', async () => {
  const h = harness()
  h.client.request(normalPlan, 'mu', 1)
  await wait(40)
  h.worker.onmessage({ data: { id: h.worker.sent[0].id, failed: true } })
  assert.equal(h.states.at(-1).failed, true)
  assert.equal(h.states.at(-1).pending, false)
  h.client.request(normalPlan, 'mu', 2)
  await wait(40)
  h.worker.answer(h.worker.sent[1].id)
  assert.equal(h.states.at(-1).failed, false)
  h.client.dispose()
})

test('a worker that cannot start is a failure, and is tried again', async () => {
  const states = []
  let attempts = 0
  const worker = fakeWorker()
  const quiet = console.error
  console.error = () => {}
  const client = bayesPriorPreviewClient(async () => { if (++attempts === 1) throw new Error('no chunk'); return worker }, s => states.push(s), 5)
  try {
    client.request(normalPlan, 'mu', 1)
    await wait(30)
    assert.equal(states.at(-1).failed, true)
    client.request(normalPlan, 'mu', 2)
    await wait(30)
  } finally { console.error = quiet }
  assert.equal(worker.sent.length, 1)
  client.dispose()
})

test('a plan that postMessage cannot clone is sent as a plain copy', async () => {
  const h = harness()
  const plan = { ...normalPlan, hook: () => 1 }
  h.client.request(plan, 'mu', 1)
  await wait(40)
  assert.equal(h.worker.sent[0].plan.order[0], 'mu')
  assert.equal(h.worker.sent[0].plan.hook, undefined)
  h.client.dispose()
})

// ---------------------------------------------------------------- the entry chunk's reach

function sources (dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) sources(path, out)
    else if (/\.(vue|ts)$/.test(name)) out.push(path)
  }
  return out
}

test('only the worker imports the core and stdlib: the page side imports types of the core, nothing else', () => {
  const offenders = []
  for (const path of ['components', 'pages', 'composables', 'plugins', 'middleware', 'layouts'].filter(d => statSync(join(FRONTEND, d), { throwIfNoEntry: false })).flatMap(d => sources(join(FRONTEND, d)))) {
    const rel = relative(FRONTEND, path)
    if (/bayesPriorCore|bayesDistributions|bayesPrior\.worker/.test(rel)) continue
    const text = readFileSync(path, 'utf8')
    for (const m of text.matchAll(/^\s*(import\b[^\n]*|.*\bimport\(\s*[^)]*\))/gm)) {
      const line = m[0]
      if (/^\s*import\s+type\b/.test(line)) continue
      if (/bayesPriorCore|bayesDistributions|@stdlib|bayesPrior\.worker/.test(line) && !/\?worker/.test(line)) offenders.push(`${rel}: ${line.trim()}`)
      if (/@stdlib/.test(line)) offenders.push(`${rel}: ${line.trim()}`)
    }
  }
  assert.deepEqual(offenders, [])
  const page = readFileSync(join(FRONTEND, 'composables/bayesPriorPreview.ts'), 'utf8')
  assert.match(page, /import\('~\/composables\/bayesPrior\.worker\?worker'\)/, 'the worker is loaded lazily')
  assert.doesNotMatch(page, /^import (?!type)[^\n]*bayesPriorCore/m)
})

// ---------------------------------------------------------------- the words

const catalogs = { en: enGen, it: itGen, de: deGen, es: esGen, fr: frGen }
const leaf = (tree, prefix = '', out = {}) => {
  for (const [k, v] of Object.entries(tree)) typeof v === 'string' ? (out[`${prefix}${k}`] = v) : leaf(v, `${prefix}${k}.`, out)
  return out
}
const PHRASES = {
  summary: ['name', 'low', 'high', 'median'], exact: [], mixture: ['n', 'name'], truncated: [], quantiles: ['q5', 'q50', 'q95'],
  'axis.curve': [], 'axis.histogram': [], 'cut.curve': ['below', 'above'], 'cut.histogram': ['below', 'above'],
  'leftOut.argument': ['count', 'total', 'node'], 'leftOut.not_finite': ['count', 'total', 'node'], 'leftOut.no_mass': ['count', 'total', 'node'],
  'none.needs_data': [], 'none.errors': [], 'none.no_mass': [], 'none.constant': ['value'],
  drawAgain: [], seed: ['seed'], drawing: [], failed: [],
}

test('every phrase of bayes.prior exists in the five languages, with the parameters the code passes', () => {
  for (const [lang, catalog] of Object.entries(catalogs)) {
    const words = leaf(catalog.bayes.prior)
    assert.deepEqual(Object.keys(words).sort(), Object.keys(PHRASES).sort(), lang)
    for (const [key, params] of Object.entries(PHRASES)) {
      const found = [...words[key].matchAll(/\{(\w+)\}/g)].map(m => m[1])
      assert.deepEqual([...new Set(found)].sort(), [...params].sort(), `${lang} ${key}`)
      assert.doesNotMatch(words[key], /[|@]/, `${lang} ${key}`)
    }
  }
})

test('no phrase of the preview judges: a measure is said as a number', () => {
  const verdicts = /\b(wide|narrow|vague|informative|reasonable|weak|strong|significant|plausible|good|bad|large|small)\b|\b(ampio|ampia|stretto|vago|informativo|ragionevole|debole|forte|significativo|plausibile)\b|\b(breit|schmal|vage|informativ|vernünftig|schwach|stark|signifikant|plausibel)\b|\b(amplio|amplia|estrecho|vago|informativo|razonable|débil|fuerte|significativo|plausible)\b|\b(large|étroit|vague|informatif|raisonnable|faible|fort|significatif|plausible)\b/i
  for (const [lang, catalog] of Object.entries(catalogs)) {
    for (const [key, text] of Object.entries(leaf(catalog.bayes.prior))) assert.doesNotMatch(text, verdicts, `${lang} ${key}`)
  }
  assert.ok(enBase)
})

test('a new request starts without the failure of the earlier one (review of the editor)', async () => {
  const states = []
  let attempts = 0
  const worker = fakeWorker()
  const quiet = console.error
  console.error = () => {}
  const client = bayesPriorPreviewClient(async () => { if (++attempts === 1) throw new Error('no chunk'); return worker }, s => states.push(s), 5)
  try {
    client.request(normalPlan, 'mu', 1)
    await wait(30)
    assert.equal(states.at(-1).failed, true)
    client.request(normalPlan, 'mu', 2)
    assert.equal(states.at(-1).pending, true)
    assert.equal(states.at(-1).failed, false, 'asked again: not failed until it fails')
    await wait(30)
  } finally { console.error = quiet }
  client.dispose()
})
