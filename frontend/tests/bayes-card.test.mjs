// The card of the group `bayes` in Administration (components/admin/BayesCardFields.vue, composables/useModelsAdmin.ts)
// without a browser: the form sends a default as null so that it follows the recipe, sizes are said the way the gateway
// says them, the fields are the ones the gateway's route takes, every word exists in the five languages, and every
// action is a native control.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { BAYES_CARD_FIELDS, bayesCardDraftOf, bayesCardInputOf, bayesBytesLabel } from '../composables/useModelsAdmin.ts'

const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const read = (p) => readFileSync(join(FRONTEND, p), 'utf8')

const defaults = { compile_cpu: '1', compile_memory: '3Gi', compile_seconds: 300, compile_tmp: '512Mi', compile_slots: 2, cache_max: '2Gi', sampling_tmp: '2Gi' }

test('what is at its default is sent as null and keeps following the recipe; the rest as typed', () => {
  assert.deepEqual(bayesCardDraftOf(defaults), { compile_cpu: '1', compile_memory: '3Gi', compile_seconds: '300', compile_tmp: '512Mi', compile_slots: '2', cache_max: '2Gi', sampling_tmp: '2Gi' })
  const same = bayesCardInputOf(bayesCardDraftOf(defaults), defaults)
  for (const k of BAYES_CARD_FIELDS) assert.equal(same[k], null, k)
  const custom = bayesCardInputOf({ ...bayesCardDraftOf(defaults), compile_memory: ' 4Gi ', compile_seconds: '600', compile_slots: '3', cache_max: '', compile_cpu: '2' }, defaults)
  assert.deepEqual(custom, { compile_cpu: '2', compile_memory: '4Gi', compile_seconds: 600, compile_tmp: null, compile_slots: 3, cache_max: null, sampling_tmp: null })
})

test('a size is said in whole GiB or MiB when exact, with one decimal otherwise', () => {
  assert.equal(bayesBytesLabel(0), '0 B')
  assert.equal(bayesBytesLabel(900), '900 B')
  assert.equal(bayesBytesLabel(2 ** 31), '2 GiB')
  assert.equal(bayesBytesLabel(512 * 2 ** 20), '512 MiB')
  assert.equal(bayesBytesLabel(2788592), '2.7 MiB')
  assert.equal(bayesBytesLabel(1.9 * 2 ** 30), '1.9 GiB')
  assert.equal(bayesBytesLabel(1536), '1.5 KiB')
})

test('the fields are the ones the gateway\'s route takes', () => {
  const go = readFileSync(join(FRONTEND, '..', 'gateway-go', 'internal', 'api', 'models_admin_bayes.go'), 'utf8')
  const body = go.slice(go.indexOf('type bayesCardIn struct'), go.indexOf('// bayesCardValues'))
  const tags = [...body.matchAll(/`json:"([a-z_]+)"`/g)].map((m) => m[1])
  assert.deepEqual([...tags].sort(), [...BAYES_CARD_FIELDS].sort())
})

function keysOf(source) {
  const start = source.indexOf('\n    "bayes": {\n      "title"')
  assert.ok(start >= 0, 'the card\'s words are not in the locale')
  const keys = []
  const path = []
  for (const line of source.slice(start + 1).split('\n').slice(1)) {
    if (/^    }/.test(line)) break
    const inline = line.match(/^\s*"([^"]+)": \{ (.*) \},?\s*$/)
    if (inline) { for (const m of inline[2].matchAll(/"([^"]+)": "/g)) keys.push([...path, inline[1], m[1]].join('.')); continue }
    const leaf = line.match(/^\s*"([^"]+)": "/)
    if (leaf) keys.push([...path, leaf[1]].join('.'))
  }
  return keys
}

test('every word the card uses exists in the five languages, with the same keys, and none holds a bare brace, bar or at-sign', () => {
  const component = read('components/admin/BayesCardFields.vue')
  const used = new Set()
  for (const m of component.matchAll(/(modelsAdmin\.bayes\.[A-Za-z_.]+)/g)) used.add(m[1].replace(/\.$/, ''))
  used.delete('modelsAdmin.bayes.rowState')
  for (const s of ['queued', 'running', 'ready', 'failed', 'timeout']) used.add(`modelsAdmin.bayes.rowState.${s}`)
  assert.ok(used.size > 30, `${used.size} keys used`)
  let reference
  for (const lang of ['en', 'it', 'fr', 'de', 'es']) {
    const source = read(`i18n/locales/${lang}.gen.ts`)
    const keys = keysOf(source)
    if (!reference) reference = keys
    assert.deepEqual([...keys].sort(), [...reference].sort(), `${lang} has the keys of en`)
    const have = new Set(keys.map((k) => `modelsAdmin.bayes.${k}`))
    for (const k of used) assert.ok(have.has(k), `${lang}: ${k} is missing`)
    // the words of the card: only {placeholders} are braces, and none has a bar or an at-sign
    const start = source.indexOf('\n    "bayes": {\n      "title"')
    const block = source.slice(start, source.indexOf('\n    },', start))
    for (const line of block.split('\n')) {
      const text = line.replace(/^[^:]+: /, '')
      if (!/^"/.test(text)) continue
      assert.doesNotMatch(text.replace(/\{[a-z]+\}/g, ''), /[{}|@]/, `${lang}: ${line.trim()}`)
    }
    assert.match(source, /"bayesCompileSettingsAction"/)
    assert.match(source, /"bayesCacheEmptyAction"/)
  }
})

test('the card is one of the group bayes\'s only, read when opened, and every action is a native control', () => {
  const section = read('components/admin/ModelsSection.vue')
  assert.match(section, /<BayesCardFields v-if="g\.id === 'bayes' && opened\[g\.id\]"/)
  const component = read('components/admin/BayesCardFields.vue')
  assert.doesNotMatch(component, /<(div|span|li|dd|dt)[^>]*@click/)
  assert.doesNotMatch(component, /tabindex=/)
  // «Empty the cache» asks first, with the sentence that says what follows
  assert.match(component, /confirmAction\(t\('modelsAdmin\.bayes\.confirmEmpty'\), 'remove'\)/)
  // each default is written beside its field and each field can go back to it
  assert.match(component, /modelsAdmin\.bayes\.default/)
  assert.match(component, /modelsAdmin\.bayes\.resetField/)
  // observers read: the fields are disabled and no button acts
  assert.match(component, /:disabled="!props\.editable"/)
  const audit = read('pages/audit.vue')
  assert.match(audit, /'models\.bayes_compile_settings'/)
  assert.match(audit, /'models\.bayes_cache_empty'/)
})
