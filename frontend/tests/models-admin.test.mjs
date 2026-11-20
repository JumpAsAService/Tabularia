// The statistical models' section of Administration without a browser (components/admin/ModelsSection.vue,
// composables/useModelsAdmin.ts): the limits form turns what the server says into what it is sent
// (an empty max_rows follows the recipe), a failure shows as one sentence, the words exist in the
// five languages for every key the section uses, and the section is one of the panel's.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { failureSentence, groupBusy, limitsDraftOf, limitsInputOf } from '../composables/useModelsAdmin.ts'

const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const read = (p) => readFileSync(join(FRONTEND, p), 'utf8')

const defaults = { cpu_limit: '2', memory_limit: '4Gi', max_timeout_seconds: 3600, max_rows: 2000000 }

test('the limits form: the recipe\'s max_rows stays empty, the longest run is in minutes', () => {
  const draft = limitsDraftOf({ limits: { ...defaults }, defaults })
  assert.deepEqual(draft, { cpu_limit: '2', memory_limit: '4Gi', minutes: 60, max_rows: '' })
  assert.deepEqual(limitsInputOf(draft), { cpu_limit: '2', memory_limit: '4Gi', max_timeout_seconds: 3600, max_rows: null })
  const custom = limitsDraftOf({ limits: { cpu_limit: '1500m', memory_limit: '3Gi', max_timeout_seconds: 1800, max_rows: 50 }, defaults })
  assert.deepEqual(custom, { cpu_limit: '1500m', memory_limit: '3Gi', minutes: 30, max_rows: '50' })
  assert.deepEqual(limitsInputOf({ ...custom, cpu_limit: ' 1 ', max_rows: ' 75 ' }),
    { cpu_limit: '1', memory_limit: '3Gi', max_timeout_seconds: 1800, max_rows: 75 })
})

test('a failure shows as its first sentence, a group is busy while built or checked', () => {
  assert.equal(failureSentence('The self-test of the group failed: 1 of 12 checks do not match.\n- linear/coef: got 1.5'),
    'The self-test of the group failed: 1 of 12 checks do not match.')
  assert.equal(failureSentence(null), '')
  for (const [state, busy] of [['building', true], ['checking', true], ['ready', false], ['failed', false], ['not_enabled', false], ['rebuild_needed', false]]) {
    assert.equal(groupBusy({ state }), busy, state)
  }
})

// the nested keys of a locale file, "a.b.c"
function keysOf(source, namespace) {
  const start = source.indexOf(`\n  "${namespace}": {`)
  assert.ok(start >= 0, `${namespace} is not in the locale`)
  const keys = []
  const path = []
  for (const line of source.slice(start + 1).split('\n').slice(1)) {
    if (/^  }/.test(line)) break
    const open = line.match(/^\s*"([^"]+)": \{\s*$/)
    if (open) { path.push(open[1]); continue }
    if (/^\s*},?\s*$/.test(line)) { path.pop(); continue }
    const inline = line.match(/^\s*"([^"]+)": \{ (.*) \},?\s*$/)
    if (inline) {
      for (const m of inline[2].matchAll(/"([^"]+)": "/g)) keys.push([...path, inline[1], m[1]].join('.'))
      continue
    }
    const leaf = line.match(/^\s*"([^"]+)": "/)
    if (leaf) keys.push([...path, leaf[1]].join('.'))
  }
  return keys
}

test('every word the section uses exists in the five languages, with the same keys', () => {
  const component = read('components/admin/ModelsSection.vue')
  const used = new Set()
  for (const m of component.matchAll(/\$?t\(\s*['"`](modelsAdmin\.[A-Za-z_.]+)['"`]/g)) used.add(m[1])
  // keys built from a state, a status, an action or a group
  const dynamic = {
    'modelsAdmin.state.': ['not_enabled', 'building', 'checking', 'ready', 'failed', 'rebuild_needed'],
    'modelsAdmin.status.': ['queued', 'building', 'checking', 'ready', 'failed'],
    'modelsAdmin.done.': ['enable', 'disable', 'rebuild'],
    'modelsAdmin.group.': ['regression.name', 'regression.description', 'survival.name', 'survival.description', 'timeseries.name', 'timeseries.description'],
  }
  for (const [prefix, tails] of Object.entries(dynamic)) for (const tail of tails) used.add(prefix + tail)
  assert.ok(used.size > 40, `${used.size} keys used`)
  let reference
  for (const lang of ['en', 'it', 'fr', 'de', 'es']) {
    const keys = keysOf(read(`i18n/locales/${lang}.gen.ts`), 'modelsAdmin').map((k) => `modelsAdmin.${k}`)
    if (!reference) reference = keys
    assert.deepEqual([...keys].sort(), [...reference].sort(), `${lang} has the keys of en`)
    const have = new Set(keys)
    for (const k of used) assert.ok(have.has(k), `${lang}: ${k} is missing`)
  }
})

test('the section is one of the administration\'s, and says «requires Kubernetes» itself', () => {
  const panel = read('components/AdminPanel.vue')
  assert.match(panel, /import ModelsSection from '~\/components\/admin\/ModelsSection\.vue'/)
  assert.match(panel, /id: 'models' as Section/)
  assert.match(panel, /section === 'models'/)
  // not greyed out in the navigation: the section itself explains on Compose
  assert.doesNotMatch(panel, /id: 'models'[^\n]*off:/)
  const component = read('components/admin/ModelsSection.vue')
  assert.match(component, /off === 'kubernetes'/)
  assert.match(component, /modelsAdmin\.offKubernetes/)
})

test('every action is a native control: a button, a field, a disclosure', () => {
  const component = read('components/admin/ModelsSection.vue')
  assert.doesNotMatch(component, /<(div|span|li)[^>]*@click/, 'a click on something that is not a button')
  assert.doesNotMatch(component, /tabindex=/)
})
