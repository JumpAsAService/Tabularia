// The expanders' travel: how long it takes, and the places in the code that hold the rest (a
// moved line would bring a regression unseen: a box that jumps to full height when it has just
// closed, an arrow that turns late, an expander that moves with reduced motion).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const { expandDuration, EXPAND_CLOSING, EXPAND_INSTANT } = await import('../composables/expandMotion.ts')
const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

test('a short box is quick, a tall one takes a little more and never long', () => {
  assert.equal(expandDuration(40, false), 200)
  assert.equal(expandDuration(400, false), 247)
  assert.equal(expandDuration(5000, false), 420)
  assert.ok(expandDuration(800, false) > expandDuration(300, false))
})

test('closing is quicker than opening, and the direction of the travel does not count', () => {
  for (const px of [40, 400, 5000]) {
    assert.ok(expandDuration(px, true) < expandDuration(px, false))
    assert.equal(expandDuration(-px, true), expandDuration(px, true))
  }
})

test('the last frame is held until the body is gone, and a click turns the travel round', () => {
  const code = read('composables/expandMotion.ts')
  assert.match(code, /fill: 'forwards'/)
  assert.match(code, /animation\.currentTime = duration\s+await done\(\)\s+if \(runs\.get\(box\) === run\) halt\(box\)/)
  assert.match(code, /const close = run \? !run\.closing : isOpen/)
})

test('a busy page holds the travel still instead of skipping it: paused, moved on frame by frame', () => {
  const code = read('composables/expandMotion.ts')
  assert.match(code, /animation\.pause\(\)/)
  assert.doesNotMatch(code, /animation\.play\(\)/)
  assert.match(code, /if \(last\) at \+= Math\.min\(now - last, MAX_STEP_MS\)/)
})

test('opening or closing a settings group draws nothing again', () => {
  const page = read('components/admin/SettingsSection.vue')
  assert.match(page, /const openOnes = shallowRef<Set<string>>\(new Set\(\)\)/)
  assert.match(page, /if \(open\) openOnes\.value\.add\(id\)\s+else openOnes\.value\.delete\(id\)/)
})

test('the plugin leaves the browser alone with reduced motion and on what is not an expander', () => {
  const plugin = read('plugins/expand-motion.client.ts')
  assert.match(plugin, /if \(reduced\.matches\) return\s+const hit = expanderOf\(event\)\s+if \(!hit\) return\s+event\.preventDefault\(\)/)
})

test('the travel has its own soft curve, set in the styles', () => {
  assert.match(read('assets/main.css'), /--ease-expand: cubic-bezier\(0\.25, 0\.1, 0\.25, 1\);/)
  assert.match(read('composables/expandMotion.ts'), /getPropertyValue\('--ease-expand'\)/)
})

test('while a box closes its marker and its arrow are already those of a closed one', () => {
  assert.equal(EXPAND_CLOSING, 'data-closing')
  assert.match(read('assets/main.css'), /details\[data-closing\] > summary \{ list-style-type: disclosure-closed; \}/)
  assert.match(read('components/admin/SettingsSection.vue'), /\.box\[open\]:not\(\[data-closing\]\) > summary \.arrow/)
  assert.match(read('components/admin/AiPromptsSection.vue'), /\.agent\.open:not\(\[data-closing\]\) \.chev/)
})

test('the chat rail, which is a layout and not an expander, opts out', () => {
  assert.equal(EXPAND_INSTANT, 'data-expand-instant')
  assert.match(read('pages/chat.vue'), /<details class="rail" open data-expand-instant>/)
})
