// The expression language's browser mirror (composables/bayesExpr.ts) against the gateway's
// reading: every syntactic case of the shared corpus (the cases without an environment) gives
// the same error code, position, length and arguments, or the same Format and JSON form of the
// tree. The type checker's cases (with an environment) give the same error, or the same type and
// the same interval, every end the same double (Object.is: -0, NaN and the infinities included).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const { bayesParseExpr, bayesFormatExpr, bayesExprJson, bayesFormatNumber } = await import('~/composables/bayesExpr.ts')
const { bayesCheckExprText, bayesExprTypeText, bayesIntervalFromCatalog, bayesIntervalToCatalog } = await import('~/composables/bayesExprCheck.ts')
const { default: en } = await import('../i18n/locales/en.gen.ts')
const { default: it } = await import('../i18n/locales/it.gen.ts')
const { default: de } = await import('../i18n/locales/de.gen.ts')
const { default: es } = await import('../i18n/locales/es.gen.ts')
const { default: fr } = await import('../i18n/locales/fr.gen.ts')

const CORPUS = fileURLToPath(new URL('../../gateway-go/internal/bayesexpr/testdata/corpus/handwritten.json', import.meta.url))
const SYNTAX_CODES = [
  'too_long', 'non_ascii', 'control', 'number_form', 'number_range', 'number_underflow', 'name_case',
  'name_long', 'name_reserved', 'comment', 'hash', 'text', 'hint', 'symbol', 'empty', 'unexpected',
  'unclosed', 'call_empty', 'too_deep', 'too_big', 'lag_argument',
]
const LANGUAGES = { en, it, de, es, fr }

test('the mirror gives the gateway\'s answer on every syntactic case of the corpus', (t) => {
  if (!existsSync(CORPUS)) return t.skip('the gateway corpus is not in this checkout')
  const cases = JSON.parse(readFileSync(CORPUS, 'utf8')).cases.filter(c => c.env === undefined)
  assert.ok(cases.length > 300)
  for (const c of cases) {
    const got = bayesParseExpr(c.input)
    if (c.expect.ok) {
      assert.ok(got.ok, `${c.id}: wanted ok, got ${JSON.stringify(got.error)}`)
      assert.deepEqual(JSON.parse(JSON.stringify(bayesExprJson(got.node))), c.expect.ast, `${c.id}: ast`)
      assert.equal(bayesFormatExpr(got.node), c.expect.format, `${c.id}: format`)
    } else {
      assert.ok(!got.ok, `${c.id}: wanted ${c.expect.code}, got ok`)
      assert.deepEqual(
        { code: got.error.code, pos: got.error.pos, len: got.error.len, args: got.error.args ?? {} },
        { code: c.expect.code, pos: c.expect.pos, len: c.expect.len, args: c.expect.args ?? {} },
        `${c.id}: ${JSON.stringify(c.input.slice(0, 60))}`,
      )
    }
  }
})

test('Format reads back to the same tree', (t) => {
  if (!existsSync(CORPUS)) return t.skip('the gateway corpus is not in this checkout')
  for (const c of JSON.parse(readFileSync(CORPUS, 'utf8')).cases) {
    if (c.env !== undefined || !c.expect.ok) continue
    const text = bayesFormatExpr(bayesParseExpr(c.input).node)
    // The canonical text may exceed the byte limit (1e5 is 100000): only a text within it is read back.
    if (text.length > 1000) continue
    const again = bayesParseExpr(text)
    assert.ok(again.ok, `${c.id}: ${text}`)
    assert.equal(bayesFormatExpr(again.node), text, c.id)
  }
})

test('numbers are written as Go writes them (shortest, exponent below -4 or from 6, two digits)', () => {
  const want = { 0: '0', 1: '1', 100000: '100000', 1e6: '1e+06', 1.5e6: '1.5e+06', 123456789: '1.23456789e+08', 0.0001: '0.0001', 0.00001: '1e-05', 1.25e-7: '1.25e-07', 1e15: '1e+15', 1e100: '1e+100', 0.1: '0.1', 123.456: '123.456' }
  for (const [k, v] of Object.entries(want)) assert.equal(bayesFormatNumber(Number(k)), v)
})

test('the input is read by character code: no look-alike digit or space passes', () => {
  for (const s of ['١', '1 + 2', 'a + b', '１']) {
    const r = bayesParseExpr(s)
    assert.ok(!r.ok && (r.error.code === 'expr.non_ascii'), s)
  }
  const lone = bayesParseExpr('a + \ud800')
  assert.ok(!lone.ok && lone.error.args.codepoint === 'U+FFFD')
})

// The codes whose message depends on an argument have one message per value of it; the others
// are one text. The hints' messages are the catalogue's (bayes.expr.hint.*).
const VARIANTS = {
  non_ascii: ['plain', 'look_alike', 'invisible', 'direction'],
  number_form: ['suffix', 'trailing_dot', 'leading_dot', 'second_dot', 'exponent'],
  number_range: ['chars', 'magnitude'],
  unexpected: ['value', 'operator', 'paren', 'argument', 'index_name', 'bracket'],
}

test('every syntax code has its message in the five languages, with no syntax of vue-i18n', () => {
  const check = (where, msg) => {
    assert.equal(typeof msg, 'string', where)
    assert.ok(msg.length > 10, where)
    assert.ok(!/[|@]/.test(msg), `${where}: has | or @`)
    // Only named placeholders ({name}), no list, linked message or modifier.
    assert.ok(!/[{}]/.test(msg.replace(/\{[a-z_]+\}/g, '')), `${where}: stray brace`)
  }
  for (const [lang, tree] of Object.entries(LANGUAGES)) {
    for (const code of SYNTAX_CODES) {
      if (code === 'hint') {
        assert.equal(typeof tree.bayes.expr.hint, 'object', lang)
        continue
      }
      const node = tree.bayes.expr[code]
      if (VARIANTS[code]) {
        assert.deepEqual(Object.keys(node).sort(), [...VARIANTS[code]].sort(), `${lang}: ${code}`)
        for (const v of VARIANTS[code]) check(`${lang}: ${code}.${v}`, node[v])
      } else check(`${lang}: ${code}`, node)
    }
  }
})

// ---------------------------------------------------------------- the type checker

const CHECK_CODES = [
  'unknown_name', 'time_value', 'not_number', 'index_alone', 'needs_index', 'wrong_plate', 'not_index',
  'index_scalar', 'index_target', 'index_context', 'unknown_function', 'not_function', 'arity',
  'lag_not_series', 'lag_argument', 'lag_k',
]

/** The corpus's environments, as the gateway's tests build them: Encloses reflexive, rows enclose nothing else. */
function makeEnv (spec, context) {
  const plates = new Map(spec.plates.map(p => [p.id, p]))
  const forbid = (what, ...ps) => assert.ok(!ps.includes(''), `${what} called with the scalar plate`)
  return {
    lookup (name) {
      const n = spec.nodes.find(x => x.name === name)
      if (!n) return undefined
      return {
        id: n.id,
        role: n.role,
        type: { elem: n.type, plate: n.plate, ...(n.target ? { target: n.target } : {}) },
        interval: n.interval ? bayesIntervalFromCatalog(n.interval) : undefined,
      }
    },
    context: () => context,
    encloses (outer, inner) {
      forbid('encloses', outer, inner)
      if (outer === inner) return true
      const o = plates.get(outer)
      const i = plates.get(inner)
      if (o.kind === 'rows') return false
      if (i.kind === 'rows') return true
      if (o.kind === 'levels' || i.kind === 'levels') return false
      for (let p = i.parent, seen = 0; p && seen < plates.size; p = plates.get(p)?.parent, seen++) if (p === outer) return true
      return false
    },
    isSeries (plate) {
      forbid('isSeries', plate)
      return Boolean(plates.get(plate)?.series)
    },
    indexOf: target => spec.nodes.find(n => n.type === 'index' && n.target === target)?.name,
  }
}

const same = (a, b) => Object.is(a, b)

test('the checker gives the gateway\'s answer on every typed case of the corpus', (t) => {
  if (!existsSync(CORPUS)) return t.skip('the gateway corpus is not in this checkout')
  const file = JSON.parse(readFileSync(CORPUS, 'utf8'))
  const cases = file.cases.filter(c => c.env !== undefined)
  assert.ok(cases.length >= 100)
  const codes = new Map()
  for (const c of cases) {
    const got = bayesCheckExprText(c.input, makeEnv(file.envs[c.env.use], c.env.context))
    if (c.expect.ok) {
      assert.ok(got.ok, `${c.id}: wanted ok, got ${JSON.stringify(got.error)}`)
      assert.equal(bayesExprTypeText(got.type), c.expect.type, `${c.id}: type`)
      const want = bayesIntervalFromCatalog(c.expect.interval)
      for (const k of ['lo', 'hi']) assert.ok(same(got.interval[k], want[k]), `${c.id}: ${k} ${got.interval[k]}, want ${want[k]}`)
      for (const k of ['loOpen', 'hiOpen']) assert.equal(got.interval[k], want[k], `${c.id}: ${k}`)
      assert.deepEqual(bayesIntervalToCatalog(got.interval), c.expect.interval, `${c.id}: interval`)
    } else {
      assert.ok(!got.ok, `${c.id}: wanted ${c.expect.code}, got ok`)
      assert.deepEqual(
        { code: got.error.code, pos: got.error.pos, len: got.error.len, args: got.error.args ?? {} },
        { code: c.expect.code, pos: c.expect.pos, len: c.expect.len, args: c.expect.args ?? {} },
        `${c.id}: ${JSON.stringify(c.input.slice(0, 60))}`,
      )
      const short = c.expect.code.replace('expr.', '')
      codes.set(short, (codes.get(short) ?? 0) + 1)
    }
  }
  for (const code of CHECK_CODES) assert.ok((codes.get(code) ?? 0) >= 2, `${code}: ${codes.get(code) ?? 0} cases`)
})

test('interval arithmetic is the same IEEE steps: 0.1 + 0.2, zero times infinity, -0, overflow', () => {
  const env = makeEnv({ plates: [], nodes: [] }, '')
  const iv = src => {
    const r = bayesCheckExprText(src, env)
    assert.ok(r.ok, src)
    return r.interval
  }
  assert.equal(iv('0.1 + 0.2').lo, 0.30000000000000004)
  assert.ok(Object.is(iv('0 * 5').lo, 0))
  assert.ok(Object.is(iv('-0').lo, 0))
  assert.ok(Object.is(iv('-0 * 3').hi, 0))
  assert.equal(iv(Array(40).fill('1e15').join(' * ')).hi, Infinity)
})

test('every checker code has its message in the five languages, with no syntax of vue-i18n', () => {
  // The codes whose message depends on an argument have one text per value of it.
  const VARIANTS = {
    needs_index: ['named', 'plain'], wrong_plate: ['context', 'plain'], index_context: ['context', 'plain'],
    lag_not_series: ['context', 'plain'],
  }
  const check = (where, msg) => {
    assert.equal(typeof msg, 'string', where)
    assert.ok(msg.length > 10, where)
    assert.ok(!/[|@]/.test(msg), `${where}: has | or @`)
    assert.ok(!/[{}]/.test(msg.replace(/\{[a-z_]+\}/g, '')), `${where}: stray brace`)
  }
  for (const [lang, tree] of Object.entries(LANGUAGES)) {
    for (const code of CHECK_CODES) {
      if (code === 'lag_argument') {
        // The parser's has no reason (a string); the checker's has index, kind or plate.
        check(`${lang}: lag_argument`, tree.bayes.expr.lag_argument)
        assert.deepEqual(Object.keys(tree.bayes.expr.lag_reason).sort(), ['index', 'kind', 'plate'], lang)
        for (const r of ['index', 'kind', 'plate']) check(`${lang}: lag_reason.${r}`, tree.bayes.expr.lag_reason[r])
        continue
      }
      const node = tree.bayes.expr[code]
      if (VARIANTS[code]) {
        assert.deepEqual(Object.keys(node).sort(), [...VARIANTS[code]].sort(), `${lang}: ${code}`)
        for (const v of VARIANTS[code]) check(`${lang}: ${code}.${v}`, node[v])
      } else check(`${lang}: ${code}`, node)
    }
  }
})
