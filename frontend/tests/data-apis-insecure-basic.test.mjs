// The development switch that lets a key travel as HTTP Basic over plain HTTP
// (DATA_API__XMLA_INSECURE_BASIC): the administration shows a highlighted row while it is on,
// in the five languages, with the colours of the theme and none of its own.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8')

test('the five languages say it, and say it is for trials', async () => {
  for (const lang of ['en', 'it', 'fr', 'de', 'es']) {
    const src = read(`../i18n/locales/${lang}.gen.ts`)
    for (const key of ['capXmlaInsecure', 'xmlaInsecureBasic']) {
      assert.match(src, new RegExp(`"${key}": "[^"]{12,}"`), `${lang}: ${key}`)
    }
    assert.match(src, /"xmlaInsecureBasic": "[^"]*dataApi\.xmla\.insecureBasic/, lang)
  }
})

test('the row shows only when the switch is on, and uses the theme tokens', () => {
  const vue = read('../components/admin/DataApisSection.vue')
  assert.match(vue, /v-if="list\.xmla\.insecure_basic"[^>]*data-xmla="insecure-basic"/)
  const css = vue.slice(vue.indexOf('.caps > div.warn'), vue.indexOf('.hint {'))
  assert.match(css, /var\(--warning\)/)
  assert.doesNotMatch(css, /#[0-9a-fA-F]{3,8}\b|rgba?\(/, 'a colour written by hand')
  assert.match(read('../composables/useDataApis.ts'), /insecure_basic\?: boolean/)
})
