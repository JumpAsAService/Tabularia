// The words of the editor of Bayesian models (docs/specs/bayes-graph-editor.md, «Languages»): every
// key of bayes.editor and every template's title and sentences in the five languages, with the same
// placeholders and no syntax of vue-i18n; no word that judges; and every finding of the mirror's
// corpus has its message (composables/bayesEditorText.ts chooses the key), with the plates named by
// their labels.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

register('./alias-hooks.mjs', import.meta.url)
const { bayesFindingText, bayesSpanUnits, bayesPriorText, bayesBoundsText, bayesConstraintKey, bayesArgSymbol, BAYES_SAID_CONSTRAINTS } = await import('~/composables/bayesEditorText.ts')
const { bayesTemplates } = await import('~/composables/bayesTemplates.ts')
const { bayesGraphDecode, bayesGraphModel } = await import('~/composables/bayesGraph.ts')
const LANGUAGES = {
  en: (await import('../i18n/locales/en.gen.ts')).default,
  it: (await import('../i18n/locales/it.gen.ts')).default,
  fr: (await import('../i18n/locales/fr.gen.ts')).default,
  de: (await import('../i18n/locales/de.gen.ts')).default,
  es: (await import('../i18n/locales/es.gen.ts')).default,
}

const leaf = (o, p = '') => Object.entries(o).flatMap(([k, v]) => (typeof v === 'object' ? leaf(v, `${p}${k}.`) : [[`${p}${k}`, v]]))
const at = (tree, key) => key.split('.').reduce((o, k) => (o == null ? undefined : o[k]), tree)
const placeholders = (s) => [...new Set([...s.matchAll(/\{(\w+)\}/g)].map(m => m[1]))].sort()

// the judgement list of the preview's words (tests/bayes-prior-worker.test.mjs), and the comparisons
// that say a form «samples worse»
const VERDICTS = /\b(wide|narrow|vague|informative|reasonable|weak|strong|significant|plausible|good|bad|large|small|better|worse)\b|\b(ampio|ampia|stretto|vago|informativo|ragionevole|debole|forte|significativo|plausibile|peggio|meglio)\b|\b(breit|schmal|vage|informativ|vernünftig|schwach|stark|signifikant|plausibel|schlechter|besser)\b|\b(amplio|amplia|estrecho|vago|informativo|razonable|débil|fuerte|significativo|plausible|peor|mejor)\b|\b(large|étroit|vague|informatif|raisonnable|faible|fort|significatif|plausible)\b|less well|moins bien|meno bene/i

function editorAndTemplates (tree) {
  // the editor's words, and the list of the models' (lot G3: pages/bayes-models.vue)
  const out = [...leaf(tree.bayes.editor, 'bayes.editor.'), ...leaf(tree.bayesModels, 'bayesModels.')]
  for (const t of bayesTemplates()) {
    out.push([`bayes.templates.${t.id}.title`, at(tree, `bayes.templates.${t.id}.title`)])
    out.push([`bayes.templates.${t.id}.what`, at(tree, `bayes.templates.${t.id}.what`)])
  }
  return out
}

test('every key of the editor and every template\'s title and sentences, in the five languages, with the same placeholders', () => {
  const en = new Map(editorAndTemplates(LANGUAGES.en))
  for (const [lang, tree] of Object.entries(LANGUAGES)) {
    const words = new Map(editorAndTemplates(tree))
    assert.deepEqual([...words.keys()].sort(), [...en.keys()].sort(), lang)
    for (const [key, text] of words) {
      assert.equal(typeof text, 'string', `${lang} ${key}`)
      assert.ok(text.length > 0, `${lang} ${key}`)
      assert.deepEqual(placeholders(text), placeholders(en.get(key)), `${lang} ${key}`)
      assert.doesNotMatch(text, /[|@]/, `${lang} ${key}: | or @`)
      assert.ok(!/[{}]/.test(text.replace(/\{[a-zA-Z_]+\}/g, '')), `${lang} ${key}: stray brace`)
    }
  }
})

test('no word of the editor, of the templates or of the centred form judges', () => {
  for (const [lang, tree] of Object.entries(LANGUAGES)) {
    const words = [...editorAndTemplates(tree), ...leaf(tree.bayes.graph.form_centred, 'bayes.graph.form_centred.')]
    for (const [key, text] of words) assert.doesNotMatch(text, VERDICTS, `${lang} ${key}`)
  }
})

test('every finding of the mirror\'s corpus and of the templates has its message in the five languages', () => {
  const corpus = JSON.parse(readFileSync(fileURLToPath(new URL('../../gateway-go/internal/bayesgraph/testdata/mirror/cases.json', import.meta.url)), 'utf8'))
  const findings = []
  for (const c of corpus.cases) {
    for (const f of c.findings ?? []) findings.push(f)
    for (const f of c.model?.findings ?? []) findings.push(f)
  }
  // a few the corpus may not hold: every expression code through the editor's own path
  const mine = new Set()
  const g = bayesGraphDecode(JSON.stringify(bayesTemplates().find(t => t.id === 'hier_nested').graph)).graph
  for (const text of ['u_d', 'u_d[s]', 'tau_s **2', '1.', '.5', 'exp(', 'foo', 'mu −1', 'lag(y, 1)', 'u_s[d]', '', 'exp(1, 2)', 'mu(1)', 'x']) {
    const us = g.nodes.find(n => n.id === 'us')
    us.prior.args.mu = { expr: text, ref: null }
    for (const f of bayesGraphModel(g).findings) { findings.push(f); mine.add(f) }
  }
  assert.ok(findings.length > 50, String(findings.length))
  const labels = { dst: 'District', sch: 'School', rows: 'Rows' }
  for (const f of findings) {
    const { key, params } = bayesFindingText(f, id => labels[id])
    for (const [lang, tree] of Object.entries(LANGUAGES)) {
      const text = at(tree, key)
      assert.equal(typeof text, 'string', `${lang}: ${f.code} → ${key}`)
      // the corpus breaks graphs on purpose (an index with no target): its findings may leave an
      // argument out; on a template every placeholder has its value
      if (mine.has(f)) for (const p of placeholders(text)) assert.ok(p in params, `${lang}: ${key} needs {${p}} (${JSON.stringify(f.args)})`)
    }
  }
})

test('the plates of a message are named by their labels, names stay names', () => {
  const t = bayesFindingText({ code: 'expr.needs_index', severity: 'error', node: 'us', slot: 'prior.mu', args: { pos: 0, len: 3, name: 'u_d', plate: 'dst', index: 'd' } }, id => ({ dst: 'District' })[id])
  assert.equal(t.key, 'bayes.expr.needs_index.named')
  assert.deepEqual(t.params, { pos: '0', len: '3', name: 'u_d', plate: 'District', index: 'd' })
  assert.equal(bayesFindingText({ code: 'expr.needs_index', severity: 'error', node: 'n', slot: 's', args: { name: 'a', plate: 'p' } }).key, 'bayes.expr.needs_index.plain')
  assert.equal(bayesFindingText({ code: 'expr.hint', severity: 'error', node: 'n', slot: 's', args: { hint: 'bayes.expr.hint.double_star', token: '**' } }).key, 'bayes.expr.hint.double_star')
  assert.equal(bayesFindingText({ code: 'graph.cycle', severity: 'error', node: 'n', slot: '', args: { names: ['a', 'b'] } }).params.names, 'a, b')
  assert.equal(bayesFindingText({ code: 'graph.form_centred', severity: 'warning', node: 'n', slot: '', args: { reason: 'bounded' } }).key, 'bayes.graph.form_centred.bounded')
})

test('an expression\'s span in code points, as the input counts its characters', () => {
  assert.deepEqual(bayesSpanUnits('mu + x', 5, 1), { start: 5, end: 6 })
  // a character outside the basic plane is one code point and two code units
  assert.deepEqual(bayesSpanUnits('𝜇 + x', 4, 1), { start: 5, end: 6 })
  assert.deepEqual(bayesSpanUnits('mu', 2, 0), { start: 2, end: 2 })
})

test('a prior, its bounds and its constraints as the canvas writes them', () => {
  assert.equal(bayesPriorText('normal', { mu: { expr: 'u_d[d]' }, sigma: { expr: 'tau_s' } }, ['mu', 'sigma']), 'normal(u_d[d], tau_s)')
  assert.equal(bayesPriorText('normal', { mu: { expr: '' } }, ['mu', 'sigma']), 'normal(…, …)')
  assert.equal(bayesBoundsText({ lower: 0 }), '≥ 0')
  assert.equal(bayesBoundsText({ upper: 1 }), '≤ 1')
  assert.equal(bayesBoundsText({ lower: 0, upper: 1 }), '0 … 1')
  assert.equal(bayesBoundsText(null), '')
  assert.equal(bayesConstraintKey('real'), null)
  assert.equal(bayesArgSymbol('sigma'), 'σ')
  assert.equal(bayesArgSymbol('k'), 'k')
  for (const c of BAYES_SAID_CONSTRAINTS) {
    for (const [lang, tree] of Object.entries(LANGUAGES)) assert.equal(typeof at(tree, bayesConstraintKey(c)), 'string', `${lang} ${c}`)
  }
})
