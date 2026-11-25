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
  assert.match(code, /animation\.currentTime = duration\s+await done\(\)\s+if \(runs\.get\(root\) === run\) halt\(root\)/)
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
})

test('the chat rail, which is a layout and not an expander, opts out', () => {
  assert.equal(EXPAND_INSTANT, 'data-expand-instant')
  assert.match(read('pages/chat.vue'), /<details class="rail" open data-expand-instant>/)
})

test('a body that comes and goes travels through ExpandBody, which draws nothing itself', () => {
  const body = read('components/ui/ExpandBody.vue')
  assert.match(body, /<Transition :css="false" @enter="enter" @leave="leave"><slot \/><\/Transition>/)
  assert.match(body, /props\.instant \? done\(\) : expandEnter\(el, done\)/)
  assert.match(body, /props\.instant \? done\(\) : expandLeave\(el, done\)/)
})

test('the hooks answer at once with reduced motion', () => {
  const code = read('composables/expandMotion.ts')
  for (const hook of ['expandEnter', 'expandLeave']) {
    assert.match(code, new RegExp(`export function ${hook}\\(el: Element, done: \\(\\) => void\\): void \\{\\s+if \\(!\\(el instanceof HTMLElement\\) \\|\\| reducedMotion\\(\\)\\) \\{ done\\(\\); return \\}`))
  }
})

test('a body is measured clipped, and takes back the gap its column gives it', () => {
  const code = read('composables/expandMotion.ts')
  assert.match(code, /if \(clip\) el\.style\.overflow = 'hidden'/)
  assert.match(code, /if \(clip\) el\.style\.overflow = overflow/)
  assert.match(code, /if \(clip\) shut\.marginTop = -gapBefore\(el\)/)
})

test('every expander that is not a <details> uses it, one body each', () => {
  const sites = {
    'components/admin/AiPromptsSection.vue': 1, 'components/admin/ModelsSection.vue': 1,
    'components/admin/ServiceAccountsSection.vue': 1, 'components/admin/WebhooksSection.vue': 1,
    'components/FlowAssistant.vue': 1, 'components/agent/AgentSteps.vue': 1,
    'components/ui/ConnectionDialog.vue': 1, 'components/ui/ExploreStart.vue': 1, 'components/ui/OpSidebar.vue': 2,
    'components/report/designer/RdSection.vue': 1, 'components/report/designer/RdPreview.vue': 1,
    'components/report/designer/RdTableProps.vue': 1, 'components/report/designer/RdSidebar.vue': 1,
    'pages/runs.vue': 1, 'pages/data-api.vue': 1, 'pages/chat.vue': 1, 'pages/bayes-models.vue': 1,
    'pages/report.vue': 1, 'pages/dashboard-design.vue': 1, 'pages/report-design.vue': 1,
    'pages/flow-group.vue': 1, 'pages/sql-script.vue': 2,
  }
  for (const [path, n] of Object.entries(sites)) {
    const page = read(path)
    assert.equal(page.split('<ExpandBody').length - 1, n, path)
    assert.equal(page.split('</ExpandBody>').length - 1, n, path)
  }
})

test('a table row that opens holds one block in its cell, which is what travels', () => {
  for (const path of ['components/admin/ServiceAccountsSection.vue', 'components/admin/WebhooksSection.vue']) {
    assert.match(read(path), /<ExpandBody><tr v-if="isOpen === \w\.id" class="detail">\s+<td colspan="5"><div>/, path)
  }
})

test('the Explore start box folds at once when its preference is read, and keeps its look until the columns are gone', () => {
  const box = read('components/ui/ExploreStart.vue')
  assert.match(box, /<ExpandBody :instant="!byHand" @after-leave="settled = true"><div v-if="!folded" class="cols">/)
  assert.match(box, /:class="\{ folded: folded && settled \}"/)
})
