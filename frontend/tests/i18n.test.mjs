// The interface's catalogs (i18n/locales) against each other and against the code:
// every language has the keys English has and no others, every message compiles with
// vue-i18n's compiler (a stray brace or @ is a compile error, and a placeholder written to
// be SHOWN disappears), the placeholders and plural forms match English, every key the
// code names exists, and every setting of Administration → Settings has its label.
// vue-tsc sees none of this: a key is a string to it.
// Run: npm test (Node strips the types of the catalogs it imports; vue-tsc, which reads the
// app's TypeScript, does not check this file, being JavaScript)
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { baseCompile } from '@intlify/message-compiler'

import enBase from '../i18n/locales/en.ts'
import enGen from '../i18n/locales/en.gen.ts'
import itBase from '../i18n/locales/it.ts'
import itGen from '../i18n/locales/it.gen.ts'
import deBase from '../i18n/locales/de.ts'
import deGen from '../i18n/locales/de.gen.ts'
import esBase from '../i18n/locales/es.ts'
import esGen from '../i18n/locales/es.gen.ts'
import frBase from '../i18n/locales/fr.ts'
import frGen from '../i18n/locales/fr.gen.ts'

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..')
const ROOT = join(FRONTEND, '..')
// as i18n/index.ts merges them: base and generated catalogs side by side
const CATALOGS = {
  en: [enBase, enGen], it: [itBase, itGen], de: [deBase, deGen], es: [esBase, esGen], fr: [frBase, frGen],
}

function flatten (tree, prefix = '', out = {}) {
  for (const [k, v] of Object.entries(tree)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (typeof v === 'string') out[key] = v
    else flatten(v, key, out)
  }
  return out
}

const flat = Object.fromEntries(
  Object.entries(CATALOGS).map(([lang, [base, gen]]) => [lang, flatten({ ...base, ...gen })]),
)
const en = flat.en
const enKeys = Object.keys(en)

// the code: components, pages, composables, plugins, middleware
function sources (dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) sources(path, out)
    else if (/\.(vue|ts)$/.test(name)) out.push(path)
  }
  return out
}
const CODE = ['components', 'pages', 'composables', 'plugins', 'middleware']
  .flatMap((d) => sources(join(FRONTEND, d)))
  .concat(join(FRONTEND, 'app.vue'))
  .map((path) => ({ path: relative(FRONTEND, path), text: readFileSync(path, 'utf8') }))

const placeholders = (message) => [...message.matchAll(/\{\s*([A-Za-z_]\w*)\s*\}/g)].map((m) => m[1]).sort()
const pluralForms = (message) => message.split(' | ').length

test('the base and the generated catalog of a language never share a namespace (the merge would drop one)', () => {
  for (const [lang, [base, gen]] of Object.entries(CATALOGS)) {
    assert.deepEqual(Object.keys(base).filter((k) => k in gen), [], lang)
  }
})

test('every language has the keys English has, and no others', () => {
  for (const [lang, messages] of Object.entries(flat)) {
    if (lang === 'en') continue
    assert.deepEqual(enKeys.filter((k) => !(k in messages)), [], `missing in ${lang}`)
    assert.deepEqual(Object.keys(messages).filter((k) => !(k in en)), [], `in ${lang} only, not in English`)
  }
})

test('every message compiles with vue-i18n', () => {
  const broken = []
  for (const [lang, messages] of Object.entries(flat)) {
    for (const [key, message] of Object.entries(messages)) {
      const errors = []
      baseCompile(message, { onError: (e) => errors.push(e.message) })
      if (errors.length) broken.push(`${lang} ${key}: ${errors[0]}`)
    }
  }
  assert.deepEqual(broken, [])
})

test('a placeholder written to be shown is a literal, not a parameter nobody passes', () => {
  // {audit_retention} in the privacy notice's hint was interpolated, with nothing: the
  // administrator read «Available placeholder:  — becomes…»
  assert.match(en['adminPanel.privacyPlaceholders'], /\{'\{'\}audit_retention\{'\}'\}/)
})

test('every language has the placeholders and the plural forms of English', () => {
  const wrong = []
  for (const [lang, messages] of Object.entries(flat)) {
    for (const key of enKeys) {
      if (!(key in messages)) continue
      if (placeholders(messages[key]).join() !== placeholders(en[key]).join()) {
        wrong.push(`${lang} ${key}: {${placeholders(messages[key])}} instead of {${placeholders(en[key])}}`)
      }
      if (pluralForms(messages[key]) !== pluralForms(en[key])) wrong.push(`${lang} ${key}: plural forms`)
    }
  }
  assert.deepEqual(wrong, [])
})

test('every key the code names exists', () => {
  const missing = []
  const call = /(?<![\w.])(?:\$t|t|te|tm|\$te)\(\s*(['"`])([^'"`]*?)\1/g
  for (const { path, text } of CODE) {
    for (const m of text.matchAll(call)) {
      const key = m[2]
      const line = text.slice(0, m.index).split('\n').length
      if (key.includes('${') || key.endsWith('.')) {
        // a key built from a value (`ns.${x}`, 'ns.' + x): at least one key under the fixed part
        const head = key.split('${')[0]
        if (!enKeys.some((k) => k.startsWith(head))) missing.push(`${path}:${line} ${key}`)
      } else if (!(key in en) && !enKeys.some((k) => k.startsWith(`${key}.`))) {
        missing.push(`${path}:${line} ${key}`)
      }
    }
    for (const m of text.matchAll(/\bkeypath="([\w.]+)"/g)) {
      if (!(m[1] in en)) missing.push(`${path} ${m[1]}`)
    }
  }
  assert.deepEqual(missing, [])
})

test('every setting of Administration → Settings has its label, help, box and error texts', () => {
  const read = (path) => readFileSync(join(ROOT, path), 'utf8')
  const items = (source) => [...source.matchAll(/^\s*Item\(\s*"([\w.]+)",\s*"(\w+)"/gm)].map((m) => ({ key: m[1], group: m[2] }))
  const gateway = items(read('gateway/app/core/overrides.py'))
  // the engine's processes per worker are in Administration → Workers, and its upload limit
  // is set with the gateway's own (gateway/app/services/engine_panel_settings.py, FROM_GATEWAY)
  const engine = items(read('backend/app/core/panel_settings.py')).filter((i) => i.group !== 'workers' && i.group !== 'upload')
  assert.ok(gateway.length > 20 && engine.length > 20, 'the items are no longer read: update this test')
  const wanted = [
    ...gateway.flatMap((i) => [`panelSettings.items.${i.key.replace(/\./g, '_')}.label`, `panelSettings.items.${i.key.replace(/\./g, '_')}.help`,
      `panelSettings.groups.${i.group}.title`, `panelSettings.groups.${i.group}.lead`]),
    ...engine.flatMap((i) => [`engineSettings.items.${i.key.replace(/\./g, '_')}.label`, `engineSettings.items.${i.key.replace(/\./g, '_')}.help`,
      `engineSettings.categories.${i.group}.title`, `engineSettings.categories.${i.group}.lead`]),
    // the refusals, by their code (components/admin/SettingsSection.vue, messageOf)
    ...['gateway/app/core/overrides.py', 'backend/app/core/panel_settings.py', 'gateway/app/routes/gateway_settings.py']
      .flatMap((p) => [...read(p).matchAll(/"code":\s*"(\w+)"/g)].map((m) => `panelSettings.errors.${m[1]}`)),
  ]
  assert.deepEqual([...new Set(wanted)].filter((k) => !(k in en)), [])
})
