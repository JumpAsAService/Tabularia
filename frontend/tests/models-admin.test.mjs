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
import {
  failureSentence, groupBusy, groupFollowed, licenceClause, licenceDraftOf, licenceInputOf, licenceReady, licenceSizeLabel, licenceSizesText,
  limitsDraftOf, limitsInputOf, recheckOf, systemLibraryLabel,
} from '../composables/useModelsAdmin.ts'

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
  for (const [state, busy] of [['building', true], ['checking', true], ['ready', false], ['failed', false], ['not_enabled', false], ['rebuild_needed', false],
    ['rechecking', false], ['recheck_retrying', false]]) {
    assert.equal(groupBusy({ state }), busy, state)
  }
})

// the version in use self-tested again with a new runner serves meanwhile: the group is not busy (it
// can be built again), but the page follows it, and the card names the recheck of the version in use
test('a recheck is followed by the page, and found on the version in use', () => {
  for (const [state, followed] of [['building', true], ['checking', true], ['rechecking', true], ['recheck_retrying', true], ['ready', false], ['failed', false]]) {
    assert.equal(groupFollowed({ state }), followed, state)
  }
  const inUse = { number: 1, current: true, recheck: 'recheck_retrying', recheck_attempts: 1, recheck_max_attempts: 6 }
  assert.equal(recheckOf({ versions: [{ number: 2, current: false, recheck: '' }, inUse] }), inUse)
  assert.equal(recheckOf({ versions: [{ number: 1, current: true, recheck: '' }] }), null)
  const component = read('components/admin/ModelsSection.vue')
  assert.match(component, /modelsAdmin\.recheckRetryingHint/)
  assert.match(component, /modelsAdmin\.recheckStoppedHint/)
  assert.match(component, /some\(groupFollowed\)/)
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
    'modelsAdmin.state.': ['not_enabled', 'building', 'checking', 'ready', 'failed', 'rebuild_needed', 'rechecking', 'recheck_retrying', 'licence_pending'],
    'modelsAdmin.recheck.': ['rechecking', 'recheck_retrying', 'recheck_stopped'],
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

// ── the licence of the weights (lot F1 of docs/specs/foundation-forecast.md) ──

const LICENCE_FILE = '../../gateway-go/internal/modelgroups/groups/foundation_forecast/licence/LICENSE.txt'
const licence = { sha256: '648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c',
  sizes: [{ id: '5m', bytes: 21e6 }, { id: '28m', bytes: 111e6 }, { id: '123m', bytes: 490e6 }, { id: '209m', bytes: 837e6 }] }

test('the licence box starts unticked with every size ticked, and sends the recipe\'s order', () => {
  const d = licenceDraftOf(licence)
  assert.deepEqual(d, { accept: false, sizes: ['5m', '28m', '123m', '209m'], open: false })
  assert.equal(licenceReady(d), false, 'the tick is required')
  assert.equal(licenceReady({ ...d, accept: true }), true)
  assert.equal(licenceReady({ ...d, accept: true, sizes: [] }), false, 'at least one size')
  assert.deepEqual(licenceInputOf(licence, { accept: true, sizes: ['209m', '28m'], open: true }),
    { licence_sha256: licence.sha256, accept: true, sizes: ['28m', '209m'] })
  assert.deepEqual(licenceSizeLabel(licence.sizes[1]), '28M · 111 MB')
  assert.equal(licenceSizesText(['28m', '123m']), '28M, 123M')
})

test('Build again ticks the sizes of the version in use; a group with none in use ticks them all (lot F9)', () => {
  assert.deepEqual(licenceDraftOf({ ...licence, sizes_in_use: ['5m', '28m'] }).sizes, ['5m', '28m'])
  // in the recipe's order, a size the recipe no longer has left out
  assert.deepEqual(licenceDraftOf({ ...licence, sizes_in_use: ['28m', '5m', '1b'] }).sizes, ['5m', '28m'])
  assert.deepEqual(licenceDraftOf({ ...licence, sizes_in_use: [] }).sizes, ['5m', '28m', '123m', '209m'])
  assert.deepEqual(licenceDraftOf({ ...licence, sizes_in_use: ['1b'] }).sizes, ['5m', '28m', '123m', '209m'])
  assert.equal(licenceDraftOf({ ...licence, sizes_in_use: ['5m'] }).accept, false, 'the tick is never kept')
})

test('the clause is point 2 of the shipped text, word for word, never retyped nor explained', () => {
  const text = readFileSync(fileURLToPath(new URL(LICENCE_FILE, import.meta.url)), 'utf8')
  const clause = licenceClause(text)
  assert.ok(clause)
  assert.match(clause, /^You are not authorized to exercise any rights under this license if the global consolidated monthly revenue/)
  assert.match(clause, /for the preceding month\./)
  assert.match(clause, /commercial license, which Mistral AI may grant you at its sole discretion/)
  assert.ok(text.includes(clause), 'a verbatim part of the shipped file')
  assert.equal(licenceClause('no numbered point'), null)
  assert.equal(licenceClause('2. Pay nothing.'), 'Pay nothing.')
})

test('the licence is quoted in English and said in one neutral sentence, never paraphrased, in the five languages', () => {
  const NEUTRAL = {
    en: "This is the clause of the licence of Mistral AI's weights; the official file is authoritative for its full text and its interpretation.",
    it: 'Questa è la clausola della licenza dei pesi di Mistral AI; il file ufficiale fa fede per il suo testo completo e per la sua interpretazione.',
    de: 'Dies ist die Klausel der Lizenz der Gewichte von Mistral AI; maßgeblich für ihren vollständigen Text und ihre Auslegung ist die offizielle Datei.',
    es: 'Esta es la cláusula de la licencia de los pesos de Mistral AI; el archivo oficial es el que prevalece para su texto completo y su interpretación.',
    fr: 'Voici la clause de la licence des poids de Mistral AI ; le fichier officiel fait foi pour son texte complet et son interprétation.',
  }
  // what would say the clause's meaning: its threshold, the kind of company, the money
  const PARAPHRASE = /\b20\b|million|milion|millón|millones|revenue|ricav|fatturat|umsatz|ingreso|chiffre|employer|datore|arbeitgeber|empleador|employeur|commercial|commerciale|kommerziell|comercial|company|azienda|unternehmen|empresa|entreprise|threshold|soglia|schwelle|umbral|seuil|legal advice|consulenza|rechtsberatung|asesoramiento|avis juridique/i
  for (const lang of ['en', 'it', 'de', 'es', 'fr']) {
    const source = read(`i18n/locales/${lang}.gen.ts`)
    const lines = source.split('\n')
    const start = lines.findIndex((l, i) => /^ {4}"licence": \{/.test(l) && lines.slice(Math.max(0, i - 400), i).some((x) => /^ {2}"modelsAdmin": \{/.test(x)))
    assert.ok(start > 0, lang)
    const block = []
    for (let i = start + 1; !/^ {4}\},?$/.test(lines[i]); i++) block.push(lines[i])
    assert.ok(block.some((l) => l.includes(JSON.stringify(NEUTRAL[lang]))), `${lang}: the neutral sentence`)
    const form = lines.find((l) => /^ {6}"licence": "(Weights|Pesos? |Pesi|Gewichte|Poids)/.test(l))
    assert.ok(form, `${lang}: the form's line`)
    for (const l of [...block, form]) assert.ok(!PARAPHRASE.test(l.replace(/^\s*"\w+": /, '')), `${lang}: ${l}`)
  }
})

test('the card: Enable waits for the tick, a build again opens the box first, the box carries the acceptance', () => {
  const c = read('components/admin/ModelsSection.vue')
  // Enable is disabled until the box is ticked and says why
  assert.match(c, /:disabled="!!busy\[g\.id\] \|\| !canGo\(g\)"\s+:title="canGo\(g\) \? undefined : \$t\('modelsAdmin\.licence\.needsTick'\)"\s+@click="act\(g, 'enable'\)"/)
  // the box is a checkbox and sizes, native controls; the tick is a field of its own with the words that say why
  assert.match(c, /<input :id="`mg-\$\{g\.id\}-accept`" v-model="licence\[g\.id\]\.accept" type="checkbox"/)
  assert.match(c, /v-model="licence\[g\.id\]\.sizes" type="checkbox"/)
  assert.match(c, /aria-describedby/)
  // the click that queues a build sends the acceptance; a disable does not
  assert.match(c, /api\[what\]\(g\.id, asks \? licenceInputOf\(g\.licence!, licence\[g\.id\]\) : undefined\)/)
  // Build again of a group with weights opens the box (and moves the focus into it), the box's own button builds, Cancel gives the focus back
  assert.match(c, /@click="askRebuild\(g\)"/)
  assert.match(c, /focusSoon\(`mg-\$\{g\.id\}-accept`\)/)
  assert.match(c, /focusSoon\(`mg-\$\{g\.id\}-rebuild`\)/)
  // the line of the version in use, and the warning of the audit's retention
  assert.match(c, /modelsAdmin\.licence\.line/)
  assert.match(c, /modelsAdmin\.licence\.retention/)
  assert.match(c, /licence_pending/)
})

test('the gateway answers what the card reads: the licence of a group with weights, and the body of Enable and Build again', () => {
  const api = read('composables/useModelsAdmin.ts')
  assert.match(api, /enable: \(id: string, body\?: ModelLicenceInput\)/)
  assert.match(api, /rebuild: \(id: string, body\?: ModelLicenceInput\)/)
  assert.match(api, /licence_pending/)
})
