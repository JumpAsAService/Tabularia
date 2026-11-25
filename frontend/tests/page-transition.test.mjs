// The page entrance: which navigations get the fade and which do not, and the places in the code
// that hold the rest (a moved line would bring a regression unseen: a fade that starts from
// nothing and flashes, one that holds the page back, one that runs on a cancelled navigation).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const { shouldAnimate } = await import('../composables/pageTransition.ts')
const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

const route = (path, record = path, meta = {}) => ({ path, matched: record === null ? [] : [{ path: record }], meta })

test('a change of page animates', () => {
  assert.equal(shouldAnimate(route('/explore'), route('/editor'), false), true)
  assert.equal(shouldAnimate(route('/flows'), route('/runs'), false), true)
})

test('the same page does not animate: a query, a hash or a parameter that only changes what it shows', () => {
  assert.equal(shouldAnimate(route('/bayes-model'), route('/bayes-model'), false), false)
  assert.equal(shouldAnimate(route('/explore'), route('/explore'), false), false)
  assert.equal(shouldAnimate(route('/share/a', '/share/:token()'), route('/share/b', '/share/:token()'), false), false)
})

test('no animation with reduced motion, on the first page or on an unknown address', () => {
  assert.equal(shouldAnimate(route('/explore'), route('/editor'), true), false)
  assert.equal(shouldAnimate(route('/', null), route('/explore'), false), false)
  assert.equal(shouldAnimate(route('/explore'), route('/nowhere', null), false), false)
})

test('a page can opt out, on arrival or on leaving', () => {
  assert.equal(shouldAnimate(route('/explore'), route('/editor', '/editor', { pageEnter: false }), false), false)
  assert.equal(shouldAnimate(route('/editor', '/editor', { pageEnter: false }), route('/explore'), false), false)
})

test('the fade starts from a visible opacity, is short, moves nothing, and is off with reduced motion', () => {
  const css = read('assets/main.css')
  const block = css.slice(css.indexOf('PAGE ENTRANCE'))
  const keyframes = block.match(/@keyframes page-enter \{[^\n]*\}/)[0]
  const from = Number(keyframes.match(/opacity: ([0-9.]+)/)[1])
  assert.ok(from >= 0.5 && from < 1, `starts at ${from}`)
  assert.doesNotMatch(keyframes, /transform|translate|filter|width|height/)
  assert.match(block, /@media \(prefers-reduced-motion: no-preference\) \{\s*html\[data-page-enter\] \.app-shell > \.content \{ animation: page-enter var\(--dur-1\) var\(--ease-out\)/)
  assert.doesNotMatch(css, /view-transition/)
})

test('the plugin looks at the end of a navigation that succeeded, and holds nothing back', () => {
  const plugin = read('plugins/page-enter.client.ts')
  assert.match(plugin, /router\.afterEach\(\(to, from, failure\) =>/)
  assert.match(plugin, /pending = !failure && shouldAnimate\(/)
  assert.doesNotMatch(plugin, /beforeResolve|beforeEach|startViewTransition/)
  assert.doesNotMatch(read('app.vue'), /pageTransition|:transition/)
  assert.doesNotMatch(read('nuxt.config.ts'), /pageTransition|viewTransition/)
})
