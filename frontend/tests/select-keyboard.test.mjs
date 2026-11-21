// The Select's keyboard: picking an option with Enter gives the focus back to the trigger, as Escape
// and Tab do, so that a person who fills a form with the keyboard goes on from the field they were in.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const select = readFileSync(join(fileURLToPath(new URL('..', import.meta.url)), 'components/ui/Select.vue'), 'utf8')

test('Enter in the list picks the option and returns the focus to the trigger', () => {
  const enter = select.slice(select.indexOf("ev.key === 'Enter') {"))
  const body = enter.slice(0, enter.indexOf('\n}\n'))
  assert.match(body, /pick\(flat\[hi\.value\]\)[\s\S]*triggerEl\.value\?\.focus\(\)/)
})

test('Escape and Tab still return the focus to the trigger', () => {
  assert.match(select, /ev\.key === 'Escape'\) \{ ev\.preventDefault\(\); close\(\); triggerEl\.value\?\.focus\(\) \}/)
  assert.match(select, /close\(\)\n\s+triggerEl\.value\?\.focus\(\)\n\s+return/)
})

test('a Select that opens in place of a command gives the focus back to it when it goes', () => {
  const root = join(fileURLToPath(new URL('..', import.meta.url)))
  const browser = readFileSync(join(root, 'components/ProjectBrowser.vue'), 'utf8')
  assert.match(browser, /@close="endMove\(r\)"/)
  assert.match(browser, /data-more="\$\{CSS\.escape\(key\)\}"/)
  assert.match(browser, /:data-more="rowKey\(r\)"/)
  // a click on another field keeps the focus it took
  assert.match(browser, /document\.activeElement !== document\.body\) return/)
  const api = readFileSync(join(root, 'pages/data-api.vue'), 'utf8')
  assert.match(api, /@close="closeAdding"/)
  assert.match(api, /ref="addButton"/)
  assert.match(api, /document\.activeElement === document\.body\) addButton\.value\?\.focus\(\)/)
  // no other Select that opens by itself is left without it
  const users = ['components', 'pages'].flatMap((d) => readdirSync(join(root, d), { recursive: true })
    .filter((f) => String(f).endsWith('.vue')).map((f) => join(d, String(f))))
    .filter((f) => /auto-open/.test(readFileSync(join(root, f), 'utf8')))
  assert.deepEqual(users.sort(), ['components/ProjectBrowser.vue', 'pages/data-api.vue'])
})
