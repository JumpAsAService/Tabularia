// The Stan code tab of the editor of Bayesian models without a browser (composables/bayesStanCode.ts,
// composables/bayesStanProgram.ts, the key of composables/bayesEditorKeys.ts): the tokenizer on every
// program of the generator's golden corpus (the tokens cover each line with no gap, no overlap, and a
// program that has no comment has no comment token), the mapping table read from the golden files (every
// identifier of the mapping is written in its program, whole word), a node's places in the program, what the
// tab shows while the graph has an error, the colours Monaco's theme is made of, and the program asked for
// at the right moments (600 ms after the last change, once for a change that moves only the drawing).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const C = await import('~/composables/bayesStanCode.ts')
const P = await import('~/composables/bayesStanProgram.ts')
const K = await import('~/composables/bayesEditorKeys.ts')
const { bayesTemplates } = await import('~/composables/bayesTemplates.ts')
const { nextTick } = await import('vue')

const GOLDEN = fileURLToPath(new URL('../../gateway-go/internal/bayescodegen/testdata/golden/', import.meta.url))
const cases = readdirSync(GOLDEN).filter(f => f.endsWith('.stan')).map(f => f.slice(0, -5))
const withMapping = cases.filter(c => readdirSync(GOLDEN).includes(`${c}.mapping.json`))
const program = (c) => readFileSync(`${GOLDEN}${c}.stan`, 'utf8')
const mapping = (c) => JSON.parse(readFileSync(`${GOLDEN}${c}.mapping.json`, 'utf8'))
const classesOf = (line, tokens) => tokens.map(t => [line.slice(t.start, t.end), t.cls])

test('the corpus is there', () => {
  assert.ok(cases.length > 30, String(cases.length))
})

test('the tokens of every golden program cover each line, in order, with no gap and no overlap', () => {
  for (const c of cases) {
    const lines = program(c).split('\n')
    const all = C.bayesStanTokenize(program(c))
    assert.equal(all.length, lines.length, c)
    lines.forEach((line, i) => {
      let at = 0
      for (const t of all[i]) {
        assert.equal(t.start, at, `${c} line ${i + 1}`)
        assert.ok(t.end > t.start, `${c} line ${i + 1}`)
        at = t.end
      }
      assert.equal(at, line.length, `${c} line ${i + 1}`)
      for (const t of all[i]) assert.notEqual(t.cls, 'comment', `${c} line ${i + 1}: the generator writes none`)
    })
  }
})

test('the blocks, types, bounds, tilde, numbers, functions, generated names and the person\'s names', () => {
  const t = (line, open) => classesOf(line, C.bayesStanTokenizeLine(line, open).tokens).filter(([, c]) => c !== 'space')
  assert.deepEqual(t('parameters {'), [['parameters', 'block'], ['{', 'punct']])
  assert.deepEqual(t('transformed parameters {'), [['transformed parameters', 'block'], ['{', 'punct']])
  assert.deepEqual(t('generated quantities {'), [['generated quantities', 'block'], ['{', 'punct']])
  assert.deepEqual(t('  real<lower=0.0> sigma;'), [
    ['real', 'type'], ['<', 'punct'], ['lower', 'bound'], ['=', 'punct'], ['0.0', 'number'], ['>', 'punct'], ['sigma', 'name'], [';', 'punct']])
  assert.deepEqual(t('  array[N] int<lower=1, upper=J_grp> g;').map(x => x[1]).join(' '),
    'type punct generated punct type punct bound punct number punct bound punct generated punct name punct')
  assert.deepEqual(t('  V_n7 ~ normal(0.0, 1.0);'), [
    ['V_n7', 'generated'], ['~', 'tilde'], ['normal', 'function'], ['(', 'punct'], ['0.0', 'number'], [',', 'punct'], ['1.0', 'number'], [');', 'punct']])
  assert.deepEqual(t('  x ~ normal(1e-3, 2.5E2) T[0, ];').map(x => x[1]).slice(4, 8), ['number', 'punct', 'number', 'punct'])
  assert.deepEqual(t('  x ~ normal(0, 1) T[0, ];').filter(x => x[0] === 'T'), [['T', 'keyword']])
  assert.deepEqual(t('  for (i in 1:N) {').filter(x => x[1] === 'keyword').map(x => x[0]), ['for', 'in'])
  assert.deepEqual(t('  mu = a + b .* x;').filter(x => x[1] === 'name').map(x => x[0]), ['mu', 'a', 'b', 'x'])
  // a name that is a type's word only inside a longer name stays a name
  assert.deepEqual(t('  real realm;').map(x => x[1]), ['type', 'name', 'punct'])
  // the model block is a block only at the start of a line
  assert.deepEqual(t('  model_x = 1;').map(x => x[1]), ['name', 'punct', 'number', 'punct'])
})

test('a comment is tokenized for robustness, a block one across lines', () => {
  const a = C.bayesStanTokenizeLine('  x = 1; // note')
  assert.equal(a.tokens[a.tokens.length - 1].cls, 'comment')
  assert.equal(a.inComment, false)
  const b = C.bayesStanTokenizeLine('  x /* open', false)
  assert.equal(b.inComment, true)
  const c = C.bayesStanTokenizeLine('still */ y', true)
  assert.equal(c.inComment, false)
  assert.deepEqual(classesOf('still */ y', c.tokens).filter(([, k]) => k !== 'space').map(x => x[1]), ['comment', 'name'])
  assert.deepEqual(C.bayesStanTokenize('a /* x\nb\nc */ d').map(l => l.map(t => t.cls).filter(k => k !== 'space')), [['name', 'comment'], ['comment'], ['comment', 'name']])
})

test('every identifier of every golden mapping is written in its program as a whole word, and the entries are read whole', () => {
  let seen = 0
  for (const c of withMapping) {
    const raw = mapping(c)
    const entries = C.bayesStanMapping(raw)
    assert.equal(entries.length, raw.length, c)
    for (const e of entries) {
      assert.ok(C.bayesStanUses(program(c), [e.stan]).length >= 1, `${c}: ${e.stan}`)
      assert.ok(C.BAYES_STAN_KINDS.includes(C.bayesStanKind(e.kind)), `${c}: ${e.kind}`)
      seen++
    }
  }
  assert.ok(seen > 300, String(seen))
})

test('every kind the golden mappings use has its words (nothing falls into «other»)', () => {
  const used = new Set()
  for (const c of withMapping) for (const e of mapping(c)) used.add(e.kind)
  for (const k of used) assert.equal(C.bayesStanKind(k), k, k)
  assert.equal(C.bayesStanKind('whatever'), 'other')
})

test('the mapping is read with care: anything but a list of entries with an identifier is empty or skipped', () => {
  assert.deepEqual(C.bayesStanMapping(null), [])
  assert.deepEqual(C.bayesStanMapping({ stan: 'x' }), [])
  assert.deepEqual(C.bayesStanMapping([null, 3, { stan: '' }, { block: 'data' }]), [])
  const [e] = C.bayesStanMapping([{ stan: 'V_a', node_id: 'a', name: '', label: 'A', renamed: 'no_name' }])
  assert.equal(e.name, null)
  assert.equal(e.kind, 'other')
  assert.equal(e.renamed, 'no_name')
})

test('a row selects its node, or the box a size belongs to; a mode flag selects nothing', () => {
  const entries = C.bayesStanMapping(mapping('hier_slopes'))
  const ids = new Set(['rows', 'grp', 'mua', 'mub', 'sigma', 'eff', 'x', 'y', 'g', 'a', 'b'])
  const exists = (id) => ids.has(id)
  const size = entries.find(e => e.stan === 'J_grp')
  assert.equal(C.bayesMapTarget(size, exists), 'grp')
  assert.equal(C.bayesMapTarget(entries.find(e => e.stan === 'Mode'), exists), null)
  assert.equal(C.bayesMapTarget(entries.find(e => e.stan === 'mu_a'), exists), 'mua')
  assert.equal(C.bayesMapTarget(entries.find(e => e.stan === 'mu_a'), () => false), null)
  // a node's identifiers: its own, its locals, its sizes
  const mine = C.bayesMapIdentifiers(entries, 'eff', exists)
  assert.ok(mine.includes('Z_eff') && mine.includes('L_eff'), mine.join())
  assert.deepEqual(C.bayesMapIdentifiers(entries, null, exists), [])
})

test('a node\'s places: whole words only, 1-based, in reading order; the lines once', () => {
  const src = 'model {\n  mu ~ normal(0, 1);\n  mu_a ~ normal(mu, 2);\n  y ~ normal(mu + mu, s);\n}'
  const uses = C.bayesStanUses(src, ['mu'])
  assert.deepEqual(uses, [
    { line: 2, start: 3, end: 5 }, { line: 3, start: 17, end: 19 }, { line: 4, start: 14, end: 16 }, { line: 4, start: 19, end: 21 },
  ])
  assert.deepEqual(C.bayesStanLines(uses), [2, 3, 4])
  assert.deepEqual(C.bayesStanUses(src, []), [])
  assert.equal(src.split('\n')[2].slice(16, 18), 'mu')
})

test('while the graph has an error the tab shows the last saved version\'s program; read only shows the version\'s own', () => {
  const v = C.bayesStanView
  assert.equal(v({ errors: 0, readonly: false, hasLive: true, hasStored: true }), 'live')
  assert.equal(v({ errors: 0, readonly: false, hasLive: false, hasStored: true }), 'stored')
  assert.equal(v({ errors: 0, readonly: false, hasLive: false, hasStored: false }), 'none')
  assert.equal(v({ errors: 2, readonly: false, hasLive: true, hasStored: true }), 'stored')
  assert.equal(v({ errors: 2, readonly: false, hasLive: true, hasStored: false }), 'none')
  assert.equal(v({ errors: 0, readonly: true, hasLive: true, hasStored: true }), 'stored')
  assert.equal(v({ errors: 0, readonly: true, hasLive: true, hasStored: false }), 'none')
})

test('the drawing is not part of what the program is made from', () => {
  const doc = bayesTemplates()[0].graph
  const moved = JSON.parse(JSON.stringify(doc))
  for (const k of Object.keys(moved.layout ?? {})) moved.layout[k] = { x: 1, y: 2, w: 3, h: 4 }
  assert.equal(C.bayesStanKey(moved), C.bayesStanKey(doc))
  const renamed = JSON.parse(JSON.stringify(doc))
  renamed.nodes[0].label = 'Another label'
  assert.notEqual(C.bayesStanKey(renamed), C.bayesStanKey(doc))
  assert.equal(C.bayesStanKey(null), '')
})

test('the browser\'s colours as Monaco\'s hex, and light or dark from the background', () => {
  assert.equal(C.bayesCssToHex('rgb(79, 140, 255)'), '#4f8cff')
  assert.equal(C.bayesCssToHex('rgba(79, 140, 255, 0.1)'), '#4f8cff1a')
  assert.equal(C.bayesCssToHex('rgba(79 140 255 / 50%)'), '#4f8cff80')
  assert.equal(C.bayesCssToHex('color(srgb 1 0 0.5)'), '#ff0080')
  assert.equal(C.bayesCssToHex('color(srgb 0 0 0 / 0.2)'), '#00000033')
  assert.equal(C.bayesCssToHex('#ABC'), '#aabbcc')
  assert.equal(C.bayesCssToHex('#0B0E14'), '#0b0e14')
  assert.equal(C.bayesCssToHex('not a colour'), null)
  assert.equal(C.bayesHexIsLight('#0b0e14'), false)
  assert.equal(C.bayesHexIsLight('#f5f7fb'), true)
  assert.equal(Math.round(C.bayesHexContrast('#000000', '#ffffff')), 21)
  assert.equal(C.bayesHexContrast('#000', '#fff'), 1)
  // a token too pale on the background gives way to the next candidate, then to the text
  const colors = { '--a': '#7aa8ff', '--b': '#2b57c4', '--text': '#222222' }
  assert.equal(C.bayesStanPick(['--a', '--b', '--text'], t => colors[t] ?? null, '#ffffff'), '#2b57c4')
  assert.equal(C.bayesStanPick(['--a', '--text'], t => colors[t] ?? null, '#ffffff'), '#222222')
  assert.equal(C.bayesStanPick(['--a', '--text'], t => colors[t] ?? null, '#0b0e14'), '#7aa8ff')
  // every class of token has its theme token, every token one of the app's custom properties
  for (const [cls, p] of Object.entries(C.BAYES_STAN_PALETTE)) assert.match(p.token, /^--[a-z0-9-]+$/, cls)
})

// ---------------------------------------------------------------- the program, asked for at the right moments

function fakeClock () {
  let now = 0
  const timers = new Map()
  let next = 1
  return {
    schedule: (fn, ms) => { const id = next++; timers.set(id, { at: now + ms, fn }); return id },
    cancel: (id) => timers.delete(id),
    advance (ms) {
      now += ms
      for (const [id, t] of [...timers].sort((a, b) => a[1].at - b[1].at)) if (t.at <= now) { timers.delete(id); t.fn() }
    },
    pending: () => timers.size,
  }
}
const answerOf = (n) => ({ ok: true, program: `data {\n  int N${n};\n}`, program_sha256: `sha${n}`, mapping: [{ stan: `N${n}`, kind: 'size' }], codegen_version: 1 })
function fakeClient (answers) {
  const calls = []
  return {
    calls,
    check: async (graph) => {
      calls.push(JSON.parse(JSON.stringify(graph)))
      const a = answers.shift()
      if (a instanceof Error) throw a
      return typeof a === 'function' ? a(graph) : a
    },
  }
}
const settle = async () => { for (let i = 0; i < 5; i++) await nextTick(); await new Promise(r => setImmediate(r)) }

test('the program is asked for 600 ms after the last change, the wait starting again at each one', async () => {
  const clock = fakeClock()
  const client = fakeClient([answerOf(1)])
  const s = P.createBayesStanProgram(client, { schedule: clock.schedule, cancel: clock.cancel })
  s.want({ n: 1 }, 'k1')
  assert.equal(s.status.value, 'waiting')
  clock.advance(500)
  s.want({ n: 2 }, 'k2')
  clock.advance(500)
  assert.equal(client.calls.length, 0, 'the second change started the wait again')
  clock.advance(100)
  await settle()
  assert.equal(client.calls.length, 1)
  assert.deepEqual(client.calls[0], { n: 2 }, 'the graph of the last change')
  assert.equal(s.status.value, 'ready')
  assert.equal(s.key.value, 'k2')
  assert.equal(s.program.value.sha256, 'sha1')
  assert.equal(s.program.value.version, null)
  assert.equal(s.program.value.codegenVersion, 1)
  assert.equal(s.program.value.mapping[0].stan, 'N1')
})

test('opening the tab asks at once; the same graph twice asks once; a change that moves only the drawing asks nothing', async () => {
  const clock = fakeClock()
  const client = fakeClient([answerOf(1), answerOf(2)])
  const s = P.createBayesStanProgram(client, { schedule: clock.schedule, cancel: clock.cancel })
  s.want({ n: 1 }, 'k1', true)
  assert.equal(s.status.value, 'checking')
  await settle()
  assert.equal(client.calls.length, 1)
  s.want({ n: 1 }, 'k1', true)
  s.want({ n: 1 }, 'k1')
  assert.equal(clock.pending(), 0)
  assert.equal(client.calls.length, 1)
  assert.equal(s.status.value, 'ready')
  s.want({ n: 2 }, 'k2')
  clock.advance(600)
  await settle()
  assert.equal(client.calls.length, 2)
  assert.equal(s.program.value.sha256, 'sha2')
})

test('an older answer never replaces a newer one', async () => {
  const clock = fakeClock()
  let release
  const slow = new Promise(r => { release = r })
  const client = fakeClient([() => slow, answerOf(2)])
  const s = P.createBayesStanProgram(client, { schedule: clock.schedule, cancel: clock.cancel })
  s.want({ n: 1 }, 'k1', true)
  s.want({ n: 2 }, 'k2', true)
  await settle()
  assert.equal(s.program.value.sha256, 'sha2')
  release(answerOf(1))
  await settle()
  assert.equal(s.program.value.sha256, 'sha2')
  assert.equal(s.key.value, 'k2')
})

test('holding drops the wait and the answer in flight; the program kept stays', async () => {
  const clock = fakeClock()
  const client = fakeClient([answerOf(1), answerOf(2)])
  const s = P.createBayesStanProgram(client, { schedule: clock.schedule, cancel: clock.cancel })
  s.want({ n: 1 }, 'k1', true)
  await settle()
  s.want({ n: 2 }, 'k2')
  s.hold()
  clock.advance(1000)
  await settle()
  assert.equal(client.calls.length, 1)
  assert.equal(s.program.value.sha256, 'sha1')
  assert.equal(s.status.value, 'ready')
  const t = P.createBayesStanProgram(fakeClient([answerOf(1)]), { schedule: clock.schedule, cancel: clock.cancel })
  t.hold()
  assert.equal(t.status.value, 'idle')
})

test('a refusal keeps its findings and the program before; a failure says so and keeps the program', async () => {
  const clock = fakeClock()
  const client = fakeClient([answerOf(1), { ok: false, findings: [{ code: 'x', severity: 'error', node: 'a' }] }, new Error('boom'), answerOf(3)])
  const s = P.createBayesStanProgram(client, { schedule: clock.schedule, cancel: clock.cancel })
  s.want({ n: 1 }, 'k1', true)
  await settle()
  s.want({ n: 2 }, 'k2', true)
  await settle()
  assert.equal(s.status.value, 'refused')
  assert.deepEqual(s.refusal.value, { key: 'k2', findings: [{ code: 'x', severity: 'error', node: 'a' }] })
  assert.equal(s.program.value.sha256, 'sha1')
  s.want({ n: 3 }, 'k3', true)
  await settle()
  assert.equal(s.status.value, 'failed')
  assert.equal(s.failure.value, 'boom')
  assert.equal(s.program.value.sha256, 'sha1')
  s.want({ n: 4 }, 'k4', true)
  await settle()
  assert.equal(s.status.value, 'ready')
  assert.equal(s.failure.value, '')
  assert.equal(s.refusal.value, null)
})

test('a saved version\'s program is adopted with no call, with its version; reset forgets everything', async () => {
  const clock = fakeClock()
  const client = fakeClient([])
  const s = P.createBayesStanProgram(client, { schedule: clock.schedule, cancel: clock.cancel })
  const stored = P.bayesStanProgramOf({ version: 4, program: 'data {}', program_sha256: 'abc', mapping: [], codegen_version: 2 })
  assert.deepEqual({ ...stored, mapping: [] }, { text: 'data {}', sha256: 'abc', mapping: [], codegenVersion: 2, version: 4 })
  s.adopt('k1', stored)
  s.want({ n: 1 }, 'k1', true)
  assert.equal(client.calls.length, 0)
  assert.equal(s.program.value.version, 4)
  assert.equal(s.status.value, 'ready')
  s.reset()
  assert.equal(s.program.value, null)
  assert.equal(s.status.value, 'idle')
  assert.equal(P.bayesStanProgramOf({ version: 1 }), null)
  assert.equal(P.bayesStanProgramOf(null), null)
  s.adopt('k', null)
  assert.equal(s.program.value, null)
})

// ---------------------------------------------------------------- the keys of the tab

test('the keys of the code tab: arrows walk the table, Esc leaves the code, nothing with a modifier', () => {
  assert.equal(K.bayesCodeKey({ key: 'ArrowUp' }), 'prev')
  assert.equal(K.bayesCodeKey({ key: 'ArrowDown' }), 'next')
  assert.equal(K.bayesCodeKey({ key: 'Escape' }), 'leave')
  assert.equal(K.bayesCodeKey({ key: 'ArrowDown', shiftKey: true }), null)
  assert.equal(K.bayesCodeKey({ key: 'ArrowDown', altKey: true }), null)
  assert.equal(K.bayesCodeKey({ key: 'ArrowDown', ctrlKey: true }), null)
  assert.equal(K.bayesCodeKey({ key: 'ArrowDown', metaKey: true }), null)
  assert.equal(K.bayesCodeKey({ key: 'Enter' }), null)
  const row = K.BAYES_KEY_TABLE.find(r => r.id === 'codeRows')
  for (const mac of [false, true]) {
    for (const w of mac ? row.mac : row.pc) {
      assert.ok(!K.bayesKeyMissingOnLaptops(w) && !K.bayesKeyForbidden(w))
      assert.ok(['prev', 'next'].includes(K.bayesCodeKey(K.bayesKeyEventOf(w, mac))))
      // a bare arrow is no key of the canvas
      assert.equal(K.bayesKeyAction(K.bayesKeyEventOf(w, mac), mac), null)
    }
  }
})

test('the code tab opens at its top: only a node chosen while it is open scrolls the table into sight, never the mounting of the editor', () => {
  const source = readFileSync(new URL('../components/bayes/BayesCodePanel.vue', import.meta.url), 'utf8')
  const code = source.replace(/^\s*\/\/.*$/gm, '')
  const calls = [...code.matchAll(/scrollIntoView/g)].length
  assert.equal(calls, 1, 'one scroll into sight, in the watch of the selected node')
  const watcher = code.slice(code.indexOf('watch(() => props.selected'))
  assert.ok(watcher.slice(0, watcher.indexOf('})')).includes('scrollIntoView'))
  // a scroll on opening moved the panel by a few pixels and left the title half under the tab bar (1440 px, dark theme)
  const mount = code.slice(code.indexOf('markUses(true)'), code.indexOf('function disposeEditor'))
  assert.ok(!mount.includes('scrollIntoView'), mount)
})
