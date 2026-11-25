// One page header for every page: `.page-head h1` is defined once, in main.css, and a page
// keeps only its own layout properties on it (never the title's size, weight or margin).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

test('.page-head h1 is defined in main.css, with the size every page shares', () => {
  const css = read('assets/main.css')
  const rule = css.match(/\.page-head h1\s*\{([^}]*)\}/)
  assert.ok(rule, '.page-head h1 is missing from assets/main.css')
  assert.match(rule[1], /font-size:\s*19px/)
  assert.match(rule[1], /margin:\s*0/)
})

test('listpage.css does not define the page head a second time', () => {
  assert.doesNotMatch(read('assets/listpage.css'), /\.page-head h1\s*\{/)
})

test('no page or component redefines the title of .page-head', () => {
  const files = [
    ...readdirSync(new URL('../pages/', import.meta.url)).map((f) => `pages/${f}`),
    ...readdirSync(new URL('../components/', import.meta.url)).filter((f) => f.endsWith('.vue')).map((f) => `components/${f}`),
  ].filter((f) => f.endsWith('.vue'))
  const bad = []
  for (const f of files) {
    for (const m of read(f).matchAll(/\.page-head h1\s*\{([^}]*)\}/g)) {
      if (/font-size|font-weight|margin/.test(m[1])) bad.push(f)
    }
  }
  assert.deepEqual(bad, [], 'these set the size, weight or margin of the shared page title')
})
