// The page transitions: which navigations animate and which do not, the names that carry an
// opened object from its row to its page's title (unique, and taken off when the transition is
// over), and the places in the code that hold the rest (a moved line would bring a regression
// unseen: a name left on the page, a transition started before the exit guards agree).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const { shouldAnimate, createObjectNames } = await import('../composables/pageTransition.ts')
const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

const route = (path, record = path, meta = {}) => ({ path, matched: record === null ? [] : [{ path: record }], meta })
const ok = { supported: true, reducedMotion: false }

test('a change of page animates', () => {
  assert.equal(shouldAnimate(route('/explore'), route('/editor'), ok), true)
  assert.equal(shouldAnimate(route('/editor'), route('/explore'), ok), true)
  assert.equal(shouldAnimate(route('/flows'), route('/runs'), ok), true)
})

test('the same page does not animate: a query, a hash or a parameter that only changes what it shows', () => {
  assert.equal(shouldAnimate(route('/bayes-model'), route('/bayes-model'), ok), false)
  assert.equal(shouldAnimate(route('/explore'), route('/explore'), ok), false)
  // a dynamic route: another token is the same page
  assert.equal(shouldAnimate(route('/share/a', '/share/:token()'), route('/share/b', '/share/:token()'), ok), false)
})

test('no animation without the API, with reduced motion, on the first page or an unknown address', () => {
  assert.equal(shouldAnimate(route('/explore'), route('/editor'), { supported: false, reducedMotion: false }), false)
  assert.equal(shouldAnimate(route('/explore'), route('/editor'), { supported: true, reducedMotion: true }), false)
  assert.equal(shouldAnimate(route('/', null), route('/explore'), ok), false) // the first navigation of a visit
  assert.equal(shouldAnimate(route('/explore'), route('/nowhere', null), ok), false)
})

test('a page can opt out, on arrival or on leaving', () => {
  assert.equal(shouldAnimate(route('/explore'), route('/editor', '/editor', { viewTransition: false }), ok), false)
  assert.equal(shouldAnimate(route('/editor', '/editor', { viewTransition: false }), route('/explore'), ok), false)
  assert.equal(shouldAnimate(route('/explore'), route('/editor', '/editor', { viewTransition: true }), ok), true)
})

const el = () => ({ style: { viewTransitionName: '' } })

test('an opened object names its row and the title of its page with one name, and takes it off after', () => {
  const names = createObjectNames()
  const row = el()
  const title = el()
  const name = names.begin(row)
  assert.match(name, /^vt-open-\d+$/)
  assert.equal(row.style.viewTransitionName, name)
  assert.equal(names.claim(title), true)
  assert.equal(title.style.viewTransitionName, name)
  // one title only: a second claimant does not get the name
  const other = el()
  assert.equal(names.claim(other), false)
  assert.equal(other.style.viewTransitionName, '')
  names.release()
  assert.equal(row.style.viewTransitionName, '')
  assert.equal(title.style.viewTransitionName, '')
  assert.equal(names.current(), null)
})

test('every transition has its own name, and a new one clears the old one first', () => {
  const names = createObjectNames()
  const a = el()
  const b = el()
  const first = names.begin(a)
  const second = names.begin(b) // the first was never released: it is now
  assert.notEqual(first, second)
  assert.equal(a.style.viewTransitionName, '')
  assert.equal(b.style.viewTransitionName, second)
})

test('without an opened row there is no name, and a title has nothing to claim', () => {
  const names = createObjectNames()
  const title = el()
  assert.equal(names.begin(null), null)
  assert.equal(names.claim(title), false)
  assert.equal(title.style.viewTransitionName, '')
})

test('release leaves alone an element that has taken another name meanwhile', () => {
  const names = createObjectNames()
  const row = el()
  names.begin(row)
  row.style.viewTransitionName = 'something-else'
  names.release()
  assert.equal(row.style.viewTransitionName, 'something-else')
})

test('every view-transition-name in the stylesheet exists only while a transition runs', () => {
  const css = read('assets/main.css').replace(/\/\*[\s\S]*?\*\//g, '')
  const rules = css.match(/[^{}]*\{[^{}]*view-transition-name[^{}]*\}/g) ?? []
  assert.ok(rules.length >= 3)
  for (const r of rules) assert.match(r.trim(), /^html\.vt-active /, r)
})

test('the transition has a reduced-motion path with no movement, and moves only opacity and transform', () => {
  const css = read('assets/main.css')
  const block = css.slice(css.indexOf('PAGE TRANSITIONS'))
  assert.match(block, /@media \(prefers-reduced-motion: reduce\) \{[^}]*animation: none !important/)
  const keyframes = block.match(/@keyframes vt-page-in \{[^\n]*\}/)[0]
  assert.match(keyframes, /opacity/)
  assert.match(keyframes, /transform: translateY\(6px\)/)
  assert.doesNotMatch(keyframes, /(width|height|top|left|margin|filter)/)
  assert.doesNotMatch(block, /bounce|elastic|ease-spring/)
})

test('the plugin starts at the last guard, after the pages have agreed to leave, and only where the API exists', () => {
  const plugin = read('plugins/page-transition.client.ts')
  assert.match(plugin, /router\.beforeResolve\(/)
  assert.doesNotMatch(plugin, /router\.beforeEach\(/)
  assert.match(plugin, /typeof doc\.startViewTransition !== 'function'\) return/)
  assert.match(plugin, /shouldAnimate\(from, to,/)
  // the class and the names are taken off when the transition ends, and a failed navigation ends it
  assert.match(plugin, /root\.classList\.remove\('vt-active'\)/)
  assert.match(plugin, /afterEach\(\(_to, _from, failure\)/)
  // no Vue transition on the pages: that one keeps two copies of the page mounted
  assert.doesNotMatch(read('app.vue'), /pageTransition|:transition/)
  assert.doesNotMatch(read('nuxt.config.ts'), /pageTransition|viewTransition/)
})
