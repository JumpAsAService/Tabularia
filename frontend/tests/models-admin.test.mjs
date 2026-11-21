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
import { failureSentence, groupBusy, limitsDraftOf, limitsInputOf, systemLibraryLabel } from '../composables/useModelsAdmin.ts'

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
    'modelsAdmin.group.': ['regression.name', 'regression.description', 'survival.name', 'survival.description', 'timeseries.name', 'timeseries.description',
      'boosting.name', 'boosting.description', 'customer_lifetime.name', 'customer_lifetime.description'], // the group's words (found missing on kind: the card showed the bare id)
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

test('the introduction of the section does not say the groups are in R only: a group may be in Python', () => {
  for (const lang of ['en', 'it', 'de', 'es', 'fr']) {
    const lines = read(`i18n/locales/${lang}.gen.ts`).split('\n').filter((l) => /^\s+"lead": ".*\(R [^)]*\b(?:Pakete|paquetes|paquets|pacchetti|packages)/.test(l))
    assert.equal(lines.length, 1, lang)
    assert.match(lines[0], /\bPython\b/, `${lang}: the introduction names Python beside R`)
    assert.doesNotMatch(lines[0], /\(R (?:and its|e i suoi|und seine|y sus|et ses)/, lang)
  }
})

// the system library of a group's image (the customer lifetime group's GSL) on its card
test('a system library is said with its name and version, and the card shows each one with its words in five languages', () => {
  assert.equal(systemLibraryLabel({ package: 'libgsl-dev', name: 'GSL', version: '2.7.1' }), 'GSL 2.7.1')
  assert.equal(systemLibraryLabel({ package: 'libfoo-dev', name: 'libfoo-dev' }), 'libfoo-dev')
  const component = read('components/admin/ModelsSection.vue')
  assert.match(component, /v-for="lib in g\.system_libraries \?\? \[\]"/)
  assert.match(component, /modelsAdmin\.systemLibrary', \{ library: systemLibraryLabel\(lib\) \}/)
  // the distribution is not written by hand: the package's whole version (which says it) is secondary text beside the name
  const expected = {
    en: ['System library: {library}', 'package {package} {version}'], it: ['Libreria di sistema: {library}', 'pacchetto {package} {version}'],
    fr: ['Bibliothèque système : {library}', 'paquet {package} {version}'], de: ['Systembibliothek: {library}', 'Paket {package} {version}'],
    es: ['Biblioteca del sistema: {library}', 'paquete {package} {version}'],
  }
  for (const [lang, [text, pkg]] of Object.entries(expected)) {
    const source = read(`i18n/locales/${lang}.gen.ts`)
    assert.ok(source.includes(`"systemLibrary": "${text}"`) && source.includes(`"systemLibraryPackage": "${pkg}"`), lang)
    assert.doesNotMatch(source, /Debian\)|\(Ubuntu/, lang)
  }
  assert.match(component, /v-if="lib\.package_version" class="system-library-package">\(\{\{ \$t\('modelsAdmin\.systemLibraryPackage', \{ package: lib\.package, version: lib\.package_version \}\) \}\}\)/)
  assert.equal(systemLibraryLabel({ package: 'libgsl-dev', name: 'GSL', version: '2.7.1', package_version: '2.7.1+dfsg-6ubuntu2' }), 'GSL 2.7.1')
  // a group with none shows nothing: the answer's list is empty (the gateway's), and an older answer without it is read as empty
  assert.match(component, /g\.system_libraries \?\? \[\]/)
})

test('Predict\'s horizon is worded for the customer lifetime models too, in five languages', () => {
  for (const lang of ['en', 'it', 'fr', 'de', 'es']) {
    const source = read(`i18n/locales/${lang}.gen.ts`)
    const hint = source.match(/\n  "predict": \{[\s\S]*?"hints": \{[\s\S]*?"horizon": "([^"]+)"/)
    assert.ok(hint && hint[1].length > 80, `${lang}: the horizon hint`)
  }
})
