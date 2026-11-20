// The Select's keyboard: picking an option with Enter gives the focus back to the trigger, as Escape
// and Tab do, so that a person who fills a form with the keyboard goes on from the field they were in.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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
